from datetime import datetime
  from typing import Optional, Any, Dict, List
  from uuid import UUID

  from pydantic import BaseModel, ConfigDict, Field
  from sqlalchemy import Column, String, DateTime, Index, ForeignKey, Text, JSON
  from sqlalchemy.dialects.postgresql import UUID as PG_UUID
  from sqlalchemy.orm import declarative_base, relationship
  from sqlalchemy.sql import func

  from .config import is_sqlite

  Base = declarative_base()

  if is_sqlite():
      from sqlalchemy import JSON
      JSON_TYPE = JSON
      VECTOR_TYPE = Text
      ARRAY_TEXT_TYPE = JSON
  else:
      from sqlalchemy.dialects.postgresql import JSONB, ARRAY
      from pgvector.sqlalchemy import Vector
      JSON_TYPE = JSONB
      VECTOR_TYPE = Vector(1536)
      ARRAY_TEXT_TYPE = ARRAY(String)


  # ---------------------------------------------------------------------------
  # ORM models
  # ---------------------------------------------------------------------------

  class SourceRecord(Base):
      __tablename__ = "sources"

      id = Column(String, primary_key=True)
      metadata_ = Column("metadata", JSON_TYPE, nullable=False, server_default='{}')
      registered_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

      events = relationship("EventRecord", back_populates="source")


  class EventRecord(Base):
      __tablename__ = "events"

      id = Column(PG_UUID(as_uuid=True), primary_key=True)
      source_id = Column(String, ForeignKey("sources.id"), nullable=False)
      type = Column(String, nullable=False)
      ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
      data = Column(JSON_TYPE, nullable=False, server_default='{}')
      embedding = Column(VECTOR_TYPE, nullable=True)

      source = relationship("SourceRecord", back_populates="events")

      __table_args__ = (
          Index('idx_events_ts', 'ts'),
          Index('idx_events_source', 'source_id'),
          Index('idx_events_type', 'type'),
      )


  class WebhookRecord(Base):
      __tablename__ = "webhooks"

      id = Column(PG_UUID(as_uuid=True), primary_key=True)
      url = Column(String, nullable=False)
      source_id = Column(String, nullable=True)
      event_types = Column(ARRAY_TEXT_TYPE, nullable=False, server_default='[]')
      secret = Column(String, nullable=True)
      created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


  class EventTypeSchemaRecord(Base):
      __tablename__ = "event_type_schemas"

      id = Column(PG_UUID(as_uuid=True), primary_key=True)
      source_id = Column(String, ForeignKey("sources.id"), nullable=False)
      type = Column(String, nullable=False)
      schema_ = Column("schema", JSON_TYPE, nullable=False)
      table_name = Column(String, nullable=False, unique=True)
      start_time = Column(DateTime(timezone=True), nullable=False)
      end_time = Column(DateTime(timezone=True), nullable=True)

      __table_args__ = (
          Index('idx_schema_source_type_active', 'source_id', 'type', 'end_time'),
      )


  # ---------------------------------------------------------------------------
  # Pydantic models
  # ---------------------------------------------------------------------------

  class SourceCreate(BaseModel):
      id: str
      metadata: Dict[str, Any] = Field(default_factory=dict)


  class EventCreate(BaseModel):
      source_id: str
      type: str = Field(min_length=1)
      data: Dict[str, Any] = Field(default_factory=dict)


  class Event(BaseModel):
      id: UUID
      source_id: str
      type: str
      ts: datetime
      data: Dict[str, Any]

      model_config = ConfigDict(from_attributes=True)


  class WebhookCreate(BaseModel):
      url: str = Field(min_length=1)
      source_id: Optional[str] = None
      event_types: List[str] = Field(default_factory=list)
      secret: Optional[str] = None


  class Webhook(BaseModel):
      id: UUID
      url: str
      source_id: Optional[str] = None
      event_types: List[str]
      secret: Optional[str] = None
      created_at: datetime

      model_config = ConfigDict(from_attributes=True)


  class SchemaFieldDefinition(BaseModel):
      type: str
      index: bool = False


  class EventTypeSchemaCreate(BaseModel):
      source_id: str
      type: str = Field(min_length=1)
      schema_: Dict[str, SchemaFieldDefinition] = Field(alias="schema")

      model_config = ConfigDict(populate_by_name=True)


  class EventTypeSchema(BaseModel):
      id: UUID
      source_id: str
      type: str
      schema_: Dict[str, Any] = Field(alias="schema")
      table_name: str
      start_time: datetime
      end_time: Optional[datetime] = None

      model_config = ConfigDict(from_attributes=True, populate_by_name=True)
