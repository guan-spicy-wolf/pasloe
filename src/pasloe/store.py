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

from .models import SourceRecord, EventRecord, LLMResponseRecord, WebhookRecord, SourceCreate, EventCreate, WebhookCreate

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
        kind=source.kind,
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


# --- Event operations ---

async def append_event(db: AsyncSession, event: EventCreate) -> EventRecord:
    source = await get_source(db, event.source_id)
    if source is None:
        source = SourceRecord(
            id=event.source_id,
            kind="client",
            metadata_={},
            registered_at=datetime.now(timezone.utc),
        )
        db.add(source)

    record = EventRecord(
        id=uuid7(),
        source_id=event.source_id,
        type=event.type,
        ts=datetime.now(timezone.utc),
        data=event.data,
        session_id=event.session_id,
    )
    db.add(record)

    # Data-to-Table Promotion for llm_response events
    if event.type == "llm_response":
        d = event.data
        total_tokens = d.get("total_tokens")
        if total_tokens is None:
            total_tokens = d["prompt_tokens"] + d["completion_tokens"]
        llm = LLMResponseRecord(
            event_id=record.id,
            model=d["model"],
            prompt_tokens=d["prompt_tokens"],
            completion_tokens=d["completion_tokens"],
            total_tokens=total_tokens,
            latency_ms=d.get("latency_ms"),
            cost=d.get("cost"),
        )
        db.add(llm)

    await db.flush()

    # Fire webhooks after successful write (non-blocking)
    asyncio.ensure_future(_fire_webhooks(db, record))

    return record


async def get_event_by_id(db: AsyncSession, event_id: UUID) -> Optional[EventRecord]:
    """Get a single event by its UUID."""
    result = await db.execute(select(EventRecord).where(EventRecord.id == event_id))
    return result.scalar_one_or_none()


async def _fire_webhooks(db: AsyncSession, event: EventRecord) -> None:
    """Fire matching webhooks asynchronously (fire-and-forget, non-blocking)."""
    try:
        result = await db.execute(select(WebhookRecord))
        webhooks = list(result.scalars().all())
    except Exception:
        return  # DB error — don't propagate

    payload = {
        "event_id": str(event.id),
        "type": event.type,
        "source_id": event.source_id,
        "ts": event.ts.isoformat(),
    }

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
                    logger.warning("Webhook delivery failed for %s (attempt %d/%d). Retrying in %.1fs: %s", 
                                   url, attempt + 1, max_retries + 1, wait_time, exc)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("Webhook delivery failed for %s after %d attempts: %s", url, max_retries + 1, exc)

    tasks = [
        asyncio.create_task(_post(wh.url))
        for wh in webhooks
        if not wh.event_types or event.type in (wh.event_types or [])
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
    session_id: Optional[UUID] = None,
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
    if session_id:
        filters.append(EventRecord.session_id == session_id)
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


# --- Webhook operations ---

async def create_webhook(db: AsyncSession, webhook: WebhookCreate) -> WebhookRecord:
    record = WebhookRecord(
        id=uuid4(),
        url=webhook.url,
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
    """Delete a webhook. Returns True if it existed, False if not found."""
    record = await get_webhook(db, webhook_id)
    if record is None:
        return False
    await db.delete(record)
    await db.flush()
    return True
