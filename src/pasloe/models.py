from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, relationship

from .config import is_sqlite


# ---------------------------------------------------------------------------
# Database-type helpers
# ---------------------------------------------------------------------------

if is_sqlite():
    from sqlalchemy import JSON as JSON_TYPE
    from sqlalchemy import Text as UUID_TYPE
else:
    from sqlalchemy.dialects.postgresql import JSONB as JSON_TYPE  # type: ignore
    from sqlalchemy.dialects.postgresql import UUID as UUID_TYPE  # type: ignore


# ---------------------------------------------------------------------------
# ORM base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class SourceRecord(Base):
    __tablename__ = "sources"

    id = Column(String, primary_key=True)
    metadata_ = Column("metadata", JSON_TYPE, server_default="{}", nullable=False)
    registered_at = Column(DateTime(timezone=True), server_default=func.now())

    events = relationship("EventRecord", back_populates="source")


class EventRecord(Base):
    __tablename__ = "events"

    id = Column(UUID_TYPE, primary_key=True)
    source_id = Column(String, ForeignKey("sources.id"), nullable=False)
    type = Column(String, nullable=False)
    ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    data = Column(JSON_TYPE, server_default="{}", nullable=False)

    source = relationship("SourceRecord", back_populates="events")

    __table_args__ = (
        Index("idx_events_ts", "ts"),
        Index("idx_events_source", "source_id"),
        Index("idx_events_type", "type"),
    )


class IngressRecord(Base):
    __tablename__ = "ingress_events"

    id = Column(UUID_TYPE, primary_key=True)
    source_id = Column(String, nullable=False)
    type = Column(String, nullable=False)
    data = Column(JSON_TYPE, server_default="{}", nullable=False)
    idempotency_key = Column(String, nullable=True)
    accepted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    committed_at = Column(DateTime(timezone=True), nullable=True)

    # Lifecycle:
    # accepted  -> committed
    # accepted rows are retried using next_attempt_at + lease fields.
    status = Column(String, server_default="accepted", nullable=False)
    attempts = Column(Integer, server_default="0", nullable=False)
    next_attempt_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_owner = Column(String, nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, server_default="", nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "idempotency_key", name="uq_ingress_source_idempotency"),
        Index("idx_ingress_status_next_attempt", "status", "next_attempt_at"),
        Index("idx_ingress_lease_until", "lease_until"),
        Index("idx_ingress_accepted_at", "accepted_at"),
    )


class OutboxRecord(Base):
    __tablename__ = "outbox_events"

    id = Column(UUID_TYPE, primary_key=True)
    event_id = Column(UUID_TYPE, nullable=False)
    source_id = Column(String, nullable=False)
    type = Column(String, nullable=False)
    data = Column(JSON_TYPE, server_default="{}", nullable=False)
    event_ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # projector / webhook
    pipeline = Column(String, nullable=False)
    status = Column(String, server_default="pending", nullable=False)
    attempts = Column(Integer, server_default="0", nullable=False)
    next_attempt_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_owner = Column(String, nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, server_default="", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", "pipeline", name="uq_outbox_event_pipeline"),
        Index("idx_outbox_pipeline_status_next_attempt", "pipeline", "status", "next_attempt_at"),
        Index("idx_outbox_lease_until", "lease_until"),
    )


class WebhookRecord(Base):
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True)
    url = Column(String, nullable=False, unique=True)
    secret = Column(String, nullable=False, server_default="")
    event_types = Column(JSON_TYPE, server_default="[]", nullable=False)
    source_filter = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    id: str
    metadata: dict = Field(default_factory=dict)


class EventCreate(BaseModel):
    source_id: str
    type: str = Field(min_length=1)
    data: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # id is UUID7. Typed as str for JSON serialisation convenience.
    # Pydantic v2 coerces UUID → str automatically. SQLite stores as text,
    # Postgres stores as native UUID; both coerce cleanly.
    id: str
    source_id: str
    type: str
    ts: datetime
    data: dict


class EventCreatedResponse(BaseModel):
    """Response model for POST /events only — extends Event with warnings.
    GET /events returns plain Event (no warnings field)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    type: str
    ts: datetime
    data: dict
    warnings: list[str] = Field(default_factory=list)
    status: str = "accepted"


class WebhookCreate(BaseModel):
    url: str
    secret: str = ""
    event_types: list[str] = Field(default_factory=list)
    source_filter: str | None = None

class WebhookResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    url: str
    has_secret: bool
    event_types: list[str]
    source_filter: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, r: "WebhookRecord") -> "WebhookResponse":
        return cls(
            id=r.id,
            url=r.url,
            has_secret=bool(r.secret),
            event_types=r.event_types or [],
            source_filter=r.source_filter,
            created_at=r.created_at,
        )
