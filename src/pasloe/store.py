from datetime import datetime, timezone
from typing import Optional, Any, Dict
from uuid import UUID, uuid4
import asyncio
import logging

import httpx
from sqlalchemy import select, func, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from .models import (
    SourceRecord, EventRecord,
    SourceCreate, EventCreate,
    # WebhookRecord, EventTypeSchemaRecord,  # TODO: Task 7 - removed in refactor
    # WebhookCreate, EventTypeSchemaCreate,  # TODO: Task 7 - removed in refactor
)
# TODO: Task 7 - promoted module removed in refactor
# from .promoted import (
#     get_or_build_table, create_promoted_table, insert_promoted_row,
#     validate_event_data, generate_table_name,
# )

logger = logging.getLogger(__name__)


class DuplicateSourceError(ValueError):
    pass


class InvalidCursorError(ValueError):
    pass


def _encode_cursor(ts: datetime, event_id: UUID) -> str:
    return f"{ts.isoformat()}|{event_id}"


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts_raw, event_id_raw = cursor.rsplit("|", 1)
        return datetime.fromisoformat(ts_raw), UUID(event_id_raw)
    except Exception as exc:
        raise InvalidCursorError("Invalid cursor format.") from exc


# --- Source operations ---

async def register_source(db: AsyncSession, source: SourceCreate) -> SourceRecord:
    record = SourceRecord(
        id=source.id,
        metadata_=source.metadata,
        registered_at=datetime.now(timezone.utc),
    )
    try:
        async with db.begin_nested():
            db.add(record)
            await db.flush()
    except IntegrityError as exc:
        raise DuplicateSourceError(f"Source '{source.id}' already registered.") from exc
    return record


async def get_source(db: AsyncSession, source_id: str) -> Optional[SourceRecord]:
    result = await db.execute(select(SourceRecord).where(SourceRecord.id == source_id))
    return result.scalar_one_or_none()


async def list_sources(db: AsyncSession) -> list[SourceRecord]:
    result = await db.execute(select(SourceRecord).order_by(SourceRecord.registered_at))
    return list(result.scalars().all())


# --- Schema operations ---

async def _get_active_schema(
    db: AsyncSession, source_id: str, type_: str
) -> Optional[EventTypeSchemaRecord]:
    result = await db.execute(
        select(EventTypeSchemaRecord).where(
            EventTypeSchemaRecord.source_id == source_id,
            EventTypeSchemaRecord.type == type_,
            EventTypeSchemaRecord.end_time.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def register_schema(
    db: AsyncSession, schema_create: EventTypeSchemaCreate
) -> EventTypeSchemaRecord:
    now = datetime.now(timezone.utc)

    source = await get_source(db, schema_create.source_id)
    if source is None:
        raise ValueError(f"Source '{schema_create.source_id}' not found. Register it first.")

    # Deactivate existing active schema
    existing = await _get_active_schema(db, schema_create.source_id, schema_create.type)
    if existing is not None:
        existing.end_time = now
        db.add(existing)

    schema_id = uuid4()
    # Convert Pydantic models to plain dicts for JSON storage
    schema_dict = {
        k: v.model_dump() if hasattr(v, 'model_dump') else v
        for k, v in schema_create.schema_.items()
    }
    table_name = generate_table_name(schema_create.source_id, schema_create.type, str(schema_id))

    record = EventTypeSchemaRecord(
        id=schema_id,
        source_id=schema_create.source_id,
        type=schema_create.type,
        schema_=schema_dict,
        table_name=table_name,
        start_time=now,
        end_time=None,
    )
    db.add(record)
    await db.flush()

    # Build and create the physical table
    from .database import get_engine
    table = get_or_build_table(table_name, schema_dict)
    await create_promoted_table(get_engine(), table)

    return record


async def list_schemas(
    db: AsyncSession,
    source_id: Optional[str] = None,
    type_: Optional[str] = None,
    active_only: bool = False,
) -> list[EventTypeSchemaRecord]:
    stmt = select(EventTypeSchemaRecord)
    if source_id:
        stmt = stmt.where(EventTypeSchemaRecord.source_id == source_id)
    if type_:
        stmt = stmt.where(EventTypeSchemaRecord.type == type_)
    if active_only:
        stmt = stmt.where(EventTypeSchemaRecord.end_time.is_(None))
    stmt = stmt.order_by(EventTypeSchemaRecord.start_time.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_schema(db: AsyncSession, schema_id: UUID) -> Optional[EventTypeSchemaRecord]:
    result = await db.execute(
        select(EventTypeSchemaRecord).where(EventTypeSchemaRecord.id == schema_id)
    )
    return result.scalar_one_or_none()


# --- Event operations ---

async def append_event(db: AsyncSession, event: EventCreate) -> EventRecord:
    source = await get_source(db, event.source_id)
    if source is None:
        raise ValueError(f"Source '{event.source_id}' not found. Register it first.")

    # Check for active schema
    schema_record = await _get_active_schema(db, event.source_id, event.type)

    # Strict validation if schema exists
    if schema_record is not None:
        validate_event_data(event.data, schema_record.schema_)

    record = EventRecord(
        id=uuid7(),
        source_id=event.source_id,
        type=event.type,
        ts=datetime.now(timezone.utc),
        data=event.data,
    )
    db.add(record)

    # Write to promoted table in same transaction
    if schema_record is not None:
        table = get_or_build_table(schema_record.table_name, schema_record.schema_)
        await insert_promoted_row(db, table, record.id, event.data, schema_record.schema_)

    await db.flush()

    # Fire webhooks (non-blocking)
    asyncio.ensure_future(_fire_webhooks(db, record))

    return record


async def get_event_by_id(db: AsyncSession, event_id: UUID) -> Optional[EventRecord]:
    result = await db.execute(select(EventRecord).where(EventRecord.id == event_id))
    return result.scalar_one_or_none()


async def _fire_webhooks(db: AsyncSession, event: EventRecord) -> None:
    try:
        result = await db.execute(select(WebhookRecord))
        webhooks = list(result.scalars().all())
    except Exception:
        return

    payload = {
        "event_id": str(event.id),
        "type": event.type,
        "source_id": event.source_id,
        "ts": event.ts.isoformat(),
    }

    def _matches(wh: WebhookRecord) -> bool:
        if wh.source_id and wh.source_id != event.source_id:
            return False
        if wh.event_types and event.type not in (wh.event_types or []):
            return False
        return True

    async def _post(url: str) -> None:
        from .config import get_settings
        settings = get_settings()
        max_retries = settings.webhook_max_retries
        backoff = settings.webhook_retry_backoff

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    logger.info("Webhook delivered successfully to %s", url)
                    return
            except Exception as exc:
                if attempt < max_retries:
                    wait_time = backoff * (2 ** attempt)
                    logger.warning(
                        "Webhook delivery failed for %s (attempt %d/%d). Retrying in %.1fs: %s",
                        url, attempt + 1, max_retries + 1, wait_time, exc,
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("Webhook delivery failed for %s after %d attempts: %s", url, max_retries + 1, exc)

    tasks = [
        asyncio.create_task(_post(wh.url))
        for wh in webhooks
        if _matches(wh)
    ]
    if tasks:
        asyncio.gather(*tasks, return_exceptions=True)


async def query_events(
    db: AsyncSession,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    source: Optional[str] = None,
    type_: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 100,
    order: str = "asc",
) -> tuple[list[EventRecord], Optional[str]]:
    stmt = select(EventRecord)
    filters = []
    if since:
        filters.append(EventRecord.ts >= since)
    if until:
        filters.append(EventRecord.ts <= until)
    if source:
        filters.append(EventRecord.source_id == source)
    if type_:
        filters.append(EventRecord.type == type_)
    if cursor:
        cursor_ts, cursor_id = _decode_cursor(cursor)
        if order == "desc":
            filters.append(
                or_(
                    EventRecord.ts < cursor_ts,
                    and_(EventRecord.ts == cursor_ts, EventRecord.id < cursor_id),
                )
            )
        else:
            filters.append(
                or_(
                    EventRecord.ts > cursor_ts,
                    and_(EventRecord.ts == cursor_ts, EventRecord.id > cursor_id),
                )
            )

    if filters:
        stmt = stmt.where(and_(*filters))

    if order == "desc":
        stmt = stmt.order_by(EventRecord.ts.desc(), EventRecord.id.desc())
    else:
        stmt = stmt.order_by(EventRecord.ts.asc(), EventRecord.id.asc())

    stmt = stmt.limit(limit + 1)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    records = rows[:limit]
    next_cursor = None
    if has_more and records:
        last = records[-1]
        next_cursor = _encode_cursor(last.ts, last.id)
    return records, next_cursor


async def get_stats(db: AsyncSession) -> Dict[str, Any]:
    total_result = await db.execute(select(func.count()).select_from(EventRecord))
    total = total_result.scalar_one()

    by_source_result = await db.execute(
        select(EventRecord.source_id, func.count().label("count"))
        .group_by(EventRecord.source_id)
        .order_by(func.count().desc())
    )
    by_source = {row.source_id: row.count for row in by_source_result}

    by_type_result = await db.execute(
        select(EventRecord.type, func.count().label("count"))
        .group_by(EventRecord.type)
        .order_by(func.count().desc())
    )
    by_type = {row.type: row.count for row in by_type_result}

    return {
        "total_events": total,
        "by_source": by_source,
        "by_type": by_type,
    }


# --- Promoted table query ---

async def query_promoted(
    db: AsyncSession,
    source_id: str,
    type_: str,
    filters: dict[str, str],
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], dict]:
    from .promoted import query_promoted_table

    schema_record = await _get_active_schema(db, source_id, type_)
    if schema_record is None:
        raise ValueError(f"No active schema for source='{source_id}', type='{type_}'")

    schema_def = schema_record.schema_
    table = get_or_build_table(schema_record.table_name, schema_def)
    rows = await query_promoted_table(db, table, schema_def, filters, limit, offset)
    return rows, schema_def


# --- Webhook operations ---

async def create_webhook(db: AsyncSession, webhook: WebhookCreate) -> WebhookRecord:
    record = WebhookRecord(
        id=uuid4(),
        url=webhook.url,
        source_id=webhook.source_id,
        event_types=webhook.event_types,
        secret=webhook.secret,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    await db.flush()
    return record


async def list_webhooks(db: AsyncSession) -> list[WebhookRecord]:
    result = await db.execute(select(WebhookRecord).order_by(WebhookRecord.created_at))
    return list(result.scalars().all())


async def get_webhook(db: AsyncSession, webhook_id: UUID) -> Optional[WebhookRecord]:
    result = await db.execute(select(WebhookRecord).where(WebhookRecord.id == webhook_id))
    return result.scalar_one_or_none()


async def delete_webhook(db: AsyncSession, webhook_id: UUID) -> bool:
    record = await get_webhook(db, webhook_id)
    if record is None:
        return False
    await db.delete(record)
    await db.flush()
    return True
