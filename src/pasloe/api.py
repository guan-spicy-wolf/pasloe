"""FastAPI router — all HTTP endpoints."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel, ConfigDict

from . import store
from .database import get_session
from .config import get_settings
from .models import (
    Event,
    EventCreate,
    EventCreatedResponse,
    SourceCreate,
    SourceRecord,
    WebhookCreate,
    WebhookResponse,
)
from .projections import ProjectionRegistry

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(
    request: Request,
    api_key: str | None = Depends(_api_key_header),
) -> None:
    expected = get_settings().api_key
    if expected and api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


_Auth = Depends(_require_api_key)


# ---------------------------------------------------------------------------
# Projection registry — injected via app state
# ---------------------------------------------------------------------------

def _get_registry(request: Request) -> ProjectionRegistry:
    return request.app.state.projection_registry


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class SourceResponse(BaseModel):
    id: str
    metadata: dict
    registered_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_record(cls, r: SourceRecord) -> "SourceResponse":
        return cls(id=r.id, metadata=r.metadata_ or {}, registered_at=r.registered_at)


@router.post("/sources", dependencies=[_Auth])
async def register_source(
    body: SourceCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> SourceResponse:
    record, created = await store.register_source(db, body)
    response.status_code = 201 if created else 200
    return SourceResponse.from_record(record)


@router.get("/sources", dependencies=[_Auth])
async def list_sources(
    db: AsyncSession = Depends(get_session),
) -> list[SourceResponse]:
    records = await store.list_sources(db)
    return [SourceResponse.from_record(r) for r in records]


@router.get("/sources/{source_id}", dependencies=[_Auth])
async def get_source(
    source_id: str,
    db: AsyncSession = Depends(get_session),
) -> SourceResponse:
    record = await store.get_source(db, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return SourceResponse.from_record(record)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@router.post("/events", status_code=202, dependencies=[_Auth])
async def append_event(
    body: EventCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> EventCreatedResponse:
    record, created = await store.accept_event(db, body)
    response.status_code = 202 if created else 200

    return EventCreatedResponse(
        id=str(record.id),
        source_id=record.source_id,
        type=record.type,
        ts=record.accepted_at,
        data=record.data,
        warnings=[],
        status="accepted",
    )


# Reserved query param names — must not collide with projection field names.
_RESERVED = {"id", "source", "type", "since", "until", "cursor", "limit", "order"}


@router.get("/events", dependencies=[_Auth])
async def query_events(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    event_id: str | None = Query(default=None, alias="id"),
    source: str | None = None,
    type_: str | None = Query(default=None, alias="type"),
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> list[Event]:
    # Extract projection-specific filters (any non-reserved query param)
    projection_filters = {
        k: v
        for k, v in request.query_params.items()
        if k not in _RESERVED
    }

    registry: ProjectionRegistry = _get_registry(request)

    try:
        records, next_cursor = await store.query_events(
            db,
            event_id=event_id,
            source=source,
            type_=type_,
            since=since,
            until=until,
            cursor=cursor,
            limit=limit,
            order=order,
            projection_filters=projection_filters or None,
            projection_registry=registry if projection_filters else None,
        )
    except store.InvalidCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if next_cursor:
        response.headers["X-Next-Cursor"] = next_cursor

    return [
        Event(
            id=str(r.id),
            source_id=r.source_id,
            type=r.type,
            ts=r.ts,
            data=r.data,
        )
        for r in records
    ]


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.post("/webhooks", dependencies=[_Auth])
async def register_webhook(
    body: WebhookCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> WebhookResponse:
    from sqlalchemy import select
    from .models import WebhookRecord
    existing = (await db.execute(
        select(WebhookRecord).where(WebhookRecord.url == body.url)
    )).scalar_one_or_none()
    record = await store.create_or_update_webhook(db, body)
    response.status_code = 200 if existing else 201
    return WebhookResponse.from_record(record)


@router.get("/webhooks", dependencies=[_Auth])
async def list_webhooks(
    db: AsyncSession = Depends(get_session),
) -> list[WebhookResponse]:
    records = await store.list_webhooks(db)
    return [WebhookResponse.from_record(r) for r in records]


@router.delete("/webhooks/{webhook_id}", status_code=204, dependencies=[_Auth])
async def delete_webhook(
    webhook_id: str,
    db: AsyncSession = Depends(get_session),
) -> None:
    deleted = await store.delete_webhook(db, webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found")


@router.get("/events/stats", dependencies=[_Auth])
async def get_stats(
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await store.get_stats(db)
