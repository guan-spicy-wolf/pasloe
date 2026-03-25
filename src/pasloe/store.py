"""Business logic layer — source/event storage and async pipeline queue ops."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from uuid_extensions import uuid7

from .models import (
    EventCreate,
    EventRecord,
    IngressRecord,
    OutboxRecord,
    SourceCreate,
    SourceRecord,
    WebhookCreate,
    WebhookRecord,
)

if TYPE_CHECKING:
    from .projections import ProjectionRegistry

logger = logging.getLogger(__name__)

INGRESS_STATUS_ACCEPTED = "accepted"
INGRESS_STATUS_COMMITTED = "committed"
OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_DONE = "done"
PIPELINE_PROJECTOR = "projector"
PIPELINE_WEBHOOK = "webhook"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

class InvalidCursorError(ValueError):
    pass


def _encode_cursor(ts: datetime, event_id: Any) -> str:
    return f"{ts.isoformat()}|{event_id}"


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        ts_str, eid = cursor.split("|", 1)
        return datetime.fromisoformat(ts_str), eid
    except (ValueError, AttributeError) as exc:
        raise InvalidCursorError(f"Invalid cursor: {cursor!r}") from exc


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _retry_delay_seconds(attempts: int, *, base: float = 1.0, max_delay: float = 60.0) -> float:
    # attempts starts at 1 on first failure.
    return min(max_delay, base * (2 ** max(attempts - 1, 0)))


# ---------------------------------------------------------------------------
# Source operations
# ---------------------------------------------------------------------------

async def register_source(
    db: AsyncSession,
    source: SourceCreate,
) -> tuple[SourceRecord, bool]:
    """Upsert a source. Returns (record, created)."""
    existing = await get_source(db, source.id)
    if existing is not None:
        existing.metadata_ = source.metadata
        await db.flush()
        return existing, False

    record = SourceRecord(id=source.id, metadata_=source.metadata)
    db.add(record)
    await db.flush()
    return record, True


async def get_source(db: AsyncSession, source_id: str) -> SourceRecord | None:
    result = await db.execute(select(SourceRecord).where(SourceRecord.id == source_id))
    return result.scalar_one_or_none()


async def list_sources(db: AsyncSession) -> list[SourceRecord]:
    result = await db.execute(select(SourceRecord).order_by(SourceRecord.registered_at))
    return list(result.scalars().all())


async def _ensure_source(db: AsyncSession, source_id: str) -> None:
    """Auto-register source with empty metadata if missing."""
    if await get_source(db, source_id) is not None:
        return
    db.add(SourceRecord(id=source_id, metadata_={}))
    try:
        await db.flush()
    except IntegrityError:
        # Another worker inserted it first.
        await db.rollback()


# ---------------------------------------------------------------------------
# Ingest log (accepted) + committed events
# ---------------------------------------------------------------------------

async def accept_event(
    db: AsyncSession,
    event: EventCreate,
) -> tuple[IngressRecord, bool]:
    """Persist one accepted event into ingress log. Returns (record, created)."""
    if event.idempotency_key:
        existing = await get_ingress_by_idempotency(
            db, source_id=event.source_id, idempotency_key=event.idempotency_key
        )
        if existing is not None:
            return existing, False

    record = IngressRecord(
        id=str(uuid7()),
        source_id=event.source_id,
        type=event.type,
        data=event.data,
        idempotency_key=event.idempotency_key,
        status=INGRESS_STATUS_ACCEPTED,
        accepted_at=_now_utc(),
        next_attempt_at=_now_utc(),
        attempts=0,
    )
    db.add(record)
    try:
        await db.flush()
        return record, True
    except IntegrityError:
        # Idempotency race; return existing event.
        await db.rollback()
        if event.idempotency_key:
            existing = await get_ingress_by_idempotency(
                db, source_id=event.source_id, idempotency_key=event.idempotency_key
            )
            if existing is not None:
                return existing, False
        raise


async def get_ingress_by_idempotency(
    db: AsyncSession,
    *,
    source_id: str,
    idempotency_key: str,
) -> IngressRecord | None:
    result = await db.execute(
        select(IngressRecord).where(
            and_(
                IngressRecord.source_id == source_id,
                IngressRecord.idempotency_key == idempotency_key,
            )
        )
    )
    return result.scalar_one_or_none()


async def claim_ingress_batch(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
) -> list[str]:
    """Lease a batch of accepted ingress rows for the committer worker."""
    now = _now_utc()
    lease_until = now + timedelta(seconds=lease_seconds)

    candidates = await db.execute(
        select(IngressRecord.id)
        .where(
            and_(
                IngressRecord.status == INGRESS_STATUS_ACCEPTED,
                IngressRecord.next_attempt_at <= now,
                or_(IngressRecord.lease_until.is_(None), IngressRecord.lease_until < now),
            )
        )
        .order_by(IngressRecord.accepted_at.asc(), IngressRecord.id.asc())
        .limit(limit)
    )

    claimed_ids: list[str] = []
    for event_id in list(candidates.scalars().all()):
        result = await db.execute(
            update(IngressRecord)
            .where(
                and_(
                    IngressRecord.id == event_id,
                    IngressRecord.status == INGRESS_STATUS_ACCEPTED,
                    or_(IngressRecord.lease_until.is_(None), IngressRecord.lease_until < now),
                )
            )
            .values(lease_owner=worker_id, lease_until=lease_until)
        )
        if result.rowcount:
            claimed_ids.append(str(event_id))

    return claimed_ids


async def get_ingress_for_worker(
    db: AsyncSession,
    *,
    event_id: str,
    worker_id: str,
) -> IngressRecord | None:
    now = _now_utc()
    result = await db.execute(
        select(IngressRecord).where(
            and_(
                IngressRecord.id == event_id,
                IngressRecord.status == INGRESS_STATUS_ACCEPTED,
                IngressRecord.lease_owner == worker_id,
                IngressRecord.lease_until.is_not(None),
                IngressRecord.lease_until >= now,
            )
        )
    )
    return result.scalar_one_or_none()


async def commit_ingress(
    db: AsyncSession,
    ingress: IngressRecord,
) -> EventRecord:
    """Commit one accepted ingress row into visible events + pipeline outbox."""
    await _ensure_source(db, ingress.source_id)

    existing = await db.execute(select(EventRecord).where(EventRecord.id == ingress.id))
    event_record = existing.scalar_one_or_none()
    if event_record is None:
        event_record = EventRecord(
            id=ingress.id,
            source_id=ingress.source_id,
            type=ingress.type,
            data=ingress.data,
            ts=ingress.accepted_at or _now_utc(),
        )
        db.add(event_record)
        await db.flush()

    ingress.status = INGRESS_STATUS_COMMITTED
    ingress.committed_at = _now_utc()
    ingress.lease_owner = None
    ingress.lease_until = None
    ingress.last_error = ""

    await _ensure_outbox_row(
        db,
        event_record=event_record,
        pipeline=PIPELINE_PROJECTOR,
    )
    await _ensure_outbox_row(
        db,
        event_record=event_record,
        pipeline=PIPELINE_WEBHOOK,
    )
    await db.flush()
    return event_record


async def mark_ingress_retry(
    db: AsyncSession,
    ingress: IngressRecord,
    *,
    error: str,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 60.0,
) -> None:
    attempts = int(ingress.attempts or 0) + 1
    delay = _retry_delay_seconds(
        attempts,
        base=base_delay_seconds,
        max_delay=max_delay_seconds,
    )
    ingress.attempts = attempts
    ingress.next_attempt_at = _now_utc() + timedelta(seconds=delay)
    ingress.lease_owner = None
    ingress.lease_until = None
    ingress.last_error = error[:2000]
    await db.flush()


async def _ensure_outbox_row(
    db: AsyncSession,
    *,
    event_record: EventRecord,
    pipeline: str,
) -> None:
    result = await db.execute(
        select(OutboxRecord).where(
            and_(
                OutboxRecord.event_id == event_record.id,
                OutboxRecord.pipeline == pipeline,
            )
        )
    )
    if result.scalar_one_or_none() is not None:
        return

    outbox_row = OutboxRecord(
        id=str(uuid7()),
        event_id=event_record.id,
        source_id=event_record.source_id,
        type=event_record.type,
        data=event_record.data,
        event_ts=event_record.ts,
        pipeline=pipeline,
        status=OUTBOX_STATUS_PENDING,
        attempts=0,
        next_attempt_at=_now_utc(),
    )
    db.add(outbox_row)
    await db.flush()


# ---------------------------------------------------------------------------
# Outbox workers (projector / webhook)
# ---------------------------------------------------------------------------

async def claim_outbox_batch(
    db: AsyncSession,
    *,
    pipeline: str,
    worker_id: str,
    limit: int,
    lease_seconds: int,
) -> list[str]:
    now = _now_utc()
    lease_until = now + timedelta(seconds=lease_seconds)
    candidates = await db.execute(
        select(OutboxRecord.id)
        .where(
            and_(
                OutboxRecord.pipeline == pipeline,
                OutboxRecord.status == OUTBOX_STATUS_PENDING,
                OutboxRecord.next_attempt_at <= now,
                or_(OutboxRecord.lease_until.is_(None), OutboxRecord.lease_until < now),
            )
        )
        .order_by(OutboxRecord.created_at.asc(), OutboxRecord.id.asc())
        .limit(limit)
    )

    claimed_ids: list[str] = []
    for outbox_id in list(candidates.scalars().all()):
        result = await db.execute(
            update(OutboxRecord)
            .where(
                and_(
                    OutboxRecord.id == outbox_id,
                    OutboxRecord.pipeline == pipeline,
                    OutboxRecord.status == OUTBOX_STATUS_PENDING,
                    or_(OutboxRecord.lease_until.is_(None), OutboxRecord.lease_until < now),
                )
            )
            .values(lease_owner=worker_id, lease_until=lease_until)
        )
        if result.rowcount:
            claimed_ids.append(str(outbox_id))
    return claimed_ids


async def get_outbox_for_worker(
    db: AsyncSession,
    *,
    outbox_id: str,
    worker_id: str,
) -> OutboxRecord | None:
    now = _now_utc()
    result = await db.execute(
        select(OutboxRecord).where(
            and_(
                OutboxRecord.id == outbox_id,
                OutboxRecord.status == OUTBOX_STATUS_PENDING,
                OutboxRecord.lease_owner == worker_id,
                OutboxRecord.lease_until.is_not(None),
                OutboxRecord.lease_until >= now,
            )
        )
    )
    return result.scalar_one_or_none()


async def mark_outbox_done(db: AsyncSession, outbox: OutboxRecord) -> None:
    outbox.status = OUTBOX_STATUS_DONE
    outbox.processed_at = _now_utc()
    outbox.lease_owner = None
    outbox.lease_until = None
    outbox.last_error = ""
    await db.flush()


async def mark_outbox_retry(
    db: AsyncSession,
    outbox: OutboxRecord,
    *,
    error: str,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 60.0,
) -> None:
    attempts = int(outbox.attempts or 0) + 1
    delay = _retry_delay_seconds(
        attempts,
        base=base_delay_seconds,
        max_delay=max_delay_seconds,
    )
    outbox.attempts = attempts
    outbox.next_attempt_at = _now_utc() + timedelta(seconds=delay)
    outbox.lease_owner = None
    outbox.lease_until = None
    outbox.last_error = error[:2000]
    await db.flush()


def outbox_event_payload(outbox: OutboxRecord) -> dict[str, Any]:
    return {
        "id": str(outbox.event_id),
        "source_id": outbox.source_id,
        "type": outbox.type,
        "ts": outbox.event_ts.isoformat(),
        "data": outbox.data,
    }


async def apply_projection_for_outbox(
    db: AsyncSession,
    outbox: OutboxRecord,
    projection_registry: "ProjectionRegistry | None",
) -> list[str]:
    if projection_registry is None:
        return []

    result = await db.execute(select(EventRecord).where(EventRecord.id == outbox.event_id))
    event_record = result.scalar_one_or_none()
    if event_record is None:
        return [f"missing committed event for projection: {outbox.event_id}"]
    return await projection_registry.on_event(db, event_record)


# ---------------------------------------------------------------------------
# Event query (committed events only)
# ---------------------------------------------------------------------------

async def query_events(
    db: AsyncSession,
    *,
    event_id: str | None = None,
    source: str | None = None,
    type_: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = 100,
    order: str = "asc",
    projection_filters: dict[str, str] | None = None,
    projection_registry: "ProjectionRegistry | None" = None,
) -> tuple[list[EventRecord], str | None]:
    """Unified event query over committed events."""
    if event_id is not None:
        result = await db.execute(select(EventRecord).where(EventRecord.id == event_id))
        record = result.scalar_one_or_none()
        return ([record] if record else []), None

    q = select(EventRecord)

    if source:
        q = q.where(EventRecord.source_id == source)
    if type_:
        q = q.where(EventRecord.type == type_)
    if since:
        q = q.where(EventRecord.ts >= since)
    if until:
        q = q.where(EventRecord.ts <= until)

    if cursor:
        cursor_ts, cursor_eid = _decode_cursor(cursor)
        if order == "asc":
            q = q.where(
                (EventRecord.ts > cursor_ts)
                | ((EventRecord.ts == cursor_ts) & (EventRecord.id > cursor_eid))
            )
        else:
            q = q.where(
                (EventRecord.ts < cursor_ts)
                | ((EventRecord.ts == cursor_ts) & (EventRecord.id < cursor_eid))
            )

    if order == "asc":
        q = q.order_by(EventRecord.ts.asc(), EventRecord.id.asc())
    else:
        q = q.order_by(EventRecord.ts.desc(), EventRecord.id.desc())

    q = q.limit(limit)
    result = await db.execute(q)
    records = list(result.scalars().all())

    next_cursor: str | None = None
    if len(records) == limit:
        last = records[-1]
        next_cursor = _encode_cursor(last.ts, last.id)

    if source and type_ and projection_registry and projection_filters:
        ids = [r.id for r in records]
        filtered_ids = await projection_registry.filter(
            source, type_, db, ids, projection_filters
        )
        id_set = set(str(i) for i in filtered_ids)
        records = [r for r in records if str(r.id) in id_set]

    return records, next_cursor


async def get_stats(db: AsyncSession) -> dict[str, Any]:
    total_result = await db.execute(select(func.count()).select_from(EventRecord))
    total = total_result.scalar_one()

    source_result = await db.execute(
        select(EventRecord.source_id, func.count()).group_by(EventRecord.source_id)
    )
    by_source = {row[0]: row[1] for row in source_result.all()}

    type_result = await db.execute(
        select(EventRecord.type, func.count()).group_by(EventRecord.type)
    )
    by_type = {row[0]: row[1] for row in type_result.all()}

    now = _now_utc()
    ingress_pending = (
        await db.execute(
            select(func.count()).select_from(IngressRecord).where(
                IngressRecord.status == INGRESS_STATUS_ACCEPTED
            )
        )
    ).scalar_one()
    outbox_pending = (
        await db.execute(
            select(func.count()).select_from(OutboxRecord).where(
                OutboxRecord.status == OUTBOX_STATUS_PENDING
            )
        )
    ).scalar_one()
    outbox_by_pipeline_rows = await db.execute(
        select(OutboxRecord.pipeline, func.count())
        .where(OutboxRecord.status == OUTBOX_STATUS_PENDING)
        .group_by(OutboxRecord.pipeline)
    )
    outbox_by_pipeline = {row[0]: row[1] for row in outbox_by_pipeline_rows.all()}

    oldest_ingress = (
        await db.execute(
            select(func.min(IngressRecord.accepted_at)).where(
                IngressRecord.status == INGRESS_STATUS_ACCEPTED
            )
        )
    ).scalar_one_or_none()

    oldest_age_s = 0.0
    if oldest_ingress:
        if oldest_ingress.tzinfo is None:
            oldest_ingress = oldest_ingress.replace(tzinfo=timezone.utc)
        oldest_age_s = max(0.0, (now - oldest_ingress).total_seconds())

    return {
        "total_events": total,
        "by_source": by_source,
        "by_type": by_type,
        "ingress_pending": ingress_pending,
        "outbox_pending": outbox_pending,
        "outbox_pending_by_pipeline": outbox_by_pipeline,
        "oldest_uncommitted_age_s": oldest_age_s,
    }


# ---------------------------------------------------------------------------
# Webhook operations
# ---------------------------------------------------------------------------

async def create_or_update_webhook(
    db: AsyncSession,
    body: WebhookCreate,
) -> WebhookRecord:
    """Upsert webhook by URL. Returns the record (created or updated)."""
    result = await db.execute(select(WebhookRecord).where(WebhookRecord.url == body.url))
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.secret = body.secret
        existing.event_types = body.event_types
        existing.source_filter = body.source_filter
        await db.flush()
        return existing
    record = WebhookRecord(
        id=str(uuid7()),
        url=body.url,
        secret=body.secret,
        event_types=body.event_types,
        source_filter=body.source_filter,
    )
    db.add(record)
    await db.flush()
    return record


async def list_webhooks(db: AsyncSession) -> list[WebhookRecord]:
    result = await db.execute(select(WebhookRecord))
    return list(result.scalars().all())


async def get_webhook(db: AsyncSession, webhook_id: str) -> WebhookRecord | None:
    result = await db.execute(select(WebhookRecord).where(WebhookRecord.id == webhook_id))
    return result.scalar_one_or_none()


async def delete_webhook(db: AsyncSession, webhook_id: str) -> bool:
    record = await get_webhook(db, webhook_id)
    if record is None:
        return False
    await db.delete(record)
    await db.flush()
    return True


async def list_webhooks_for_event(
    db: AsyncSession,
    event_type: str,
    source_id: str,
) -> list[WebhookRecord]:
    """Return webhooks that should receive this event (type + source filters)."""
    result = await db.execute(select(WebhookRecord))
    all_webhooks = list(result.scalars().all())
    matches = []
    for wh in all_webhooks:
        if wh.source_filter and wh.source_filter != source_id:
            continue
        types = wh.event_types or []
        if types and event_type not in types:
            continue
        matches.append(wh)
    return matches

