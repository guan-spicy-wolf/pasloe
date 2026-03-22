"""Business logic layer — database operations for sources and events."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uuid_extensions import uuid7

from .models import EventCreate, EventRecord, SourceCreate, SourceRecord

if TYPE_CHECKING:
    from .projections import ProjectionRegistry

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Source operations
# ---------------------------------------------------------------------------

async def register_source(
    db: AsyncSession,
    source: SourceCreate,
) -> tuple[SourceRecord, bool]:
    """
    Upsert a source.

    Returns (record, created) where created=True if newly inserted.
    """
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
    """Auto-register source with empty metadata if it doesn't exist."""
    if await get_source(db, source_id) is None:
        db.add(SourceRecord(id=source_id, metadata_={}))
        await db.flush()


# ---------------------------------------------------------------------------
# Event operations
# ---------------------------------------------------------------------------

async def append_event(
    db: AsyncSession,
    event: EventCreate,
    projection_registry: "ProjectionRegistry | None",
) -> tuple[EventRecord, list[str]]:
    """
    Append an event. Auto-registers the source if unknown.

    Returns (event_record, warnings).
    warnings is non-empty when a matching projection skipped its write
    due to extra fields in event.data.
    """
    await _ensure_source(db, event.source_id)

    record = EventRecord(
        id=str(uuid7()),
        source_id=event.source_id,
        type=event.type,
        data=event.data,
        ts=datetime.now(timezone.utc),
    )
    db.add(record)
    await db.flush()

    warnings: list[str] = []
    if projection_registry is not None:
        warnings = await projection_registry.on_event(db, record)

    return record, warnings


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
    """
    Unified event query.

    Special case: if event_id is given, returns that single event (or empty list)
    and no cursor, ignoring all other params.

    Normal flow (two-phase):
      Phase 1 — query events table with standard filters → event_ids[]
      Phase 2 — if source+type+projection all present, narrow via projection.filter()
      Fetch full records for final ids.

    Returns (records, next_cursor).
    next_cursor is present iff Phase 1 produced exactly `limit` rows.
    """
    if event_id is not None:
        result = await db.execute(
            select(EventRecord).where(EventRecord.id == event_id)
        )
        record = result.scalar_one_or_none()
        return ([record] if record else []), None

    # --- Phase 1: events table query ---
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

    # Compute next_cursor from Phase 1 result (before projection filtering)
    next_cursor: str | None = None
    if len(records) == limit:
        last = records[-1]
        next_cursor = _encode_cursor(last.ts, last.id)

    # --- Phase 2: projection filter ---
    if (
        source
        and type_
        and projection_registry
        and projection_filters
    ):
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

    return {"total_events": total, "by_source": by_source, "by_type": by_type}
