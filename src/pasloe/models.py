from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, func
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
