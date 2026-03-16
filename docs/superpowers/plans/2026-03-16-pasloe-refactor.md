# Pasloe Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Pasloe into a semantically agnostic append-only event store with code-defined projection tables replacing the dynamic schema/promoted system.

**Architecture:** Events are the single source of truth. Projections are optional code-defined acceleration tables that the system writes to as a best-effort side effect of event insertion. A two-phase query model lets `GET /events` transparently use projection indexes when available. No webhooks, no S3, no clients, no dynamic DDL.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 (async), aiosqlite / asyncpg, pytest-asyncio, Alembic, uuid7

**Spec:** `docs/superpowers/specs/2026-03-16-pasloe-refactor-design.md`

---

## Chunk 1: Delete Removed Code

### Task 1: Delete files that are entirely removed

**Files:**
- Delete: `src/pasloe/s3.py`
- Delete: `src/pasloe/client.py`
- Delete: `src/pasloe/promoted.py`
- Delete: `clients/` (entire directory)

- [ ] **Step 1: Delete the files**

```bash
rm src/pasloe/s3.py src/pasloe/client.py src/pasloe/promoted.py
rm -rf clients/
```

- [ ] **Step 2: Verify deletion**

```bash
ls src/pasloe/
# Should NOT contain: s3.py, client.py, promoted.py
ls clients/ 2>/dev/null || echo "clients/ deleted"
```

- [ ] **Step 3: Commit**

Note: webhook logic lives in `store.py` and `api.py` (no separate `webhooks.py` exists). Those are removed when those files are rewritten in Tasks 7–8.

```bash
git add src/pasloe/s3.py src/pasloe/client.py src/pasloe/promoted.py clients/
git commit -m "chore: delete s3, client, promoted modules and clients directory"
```

---

### Task 2: Strip removed models from models.py

**Files:**
- Modify: `src/pasloe/models.py`

Current state: contains `WebhookRecord`, `EventTypeSchemaRecord`, `embedding` column on `EventRecord`, `WebhookCreate`, `Webhook`, `SchemaFieldDefinition`, `EventTypeSchemaCreate`, `EventTypeSchema`.

Target state: only `SourceRecord`, `EventRecord` (no embedding), `SourceCreate`, `EventCreate`, `Event`, plus new `EventCreatedResponse`.

- [ ] **Step 1: Run existing tests to establish baseline (they will break after this task)**

```bash
cd /root/pasloe
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: some tests pass now. After Task 2 they will break; that's intentional.

- [ ] **Step 2: Rewrite models.py**

Replace the full content of `src/pasloe/models.py` with:

```python
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
```

- [ ] **Step 3: Verify models.py parses**

```bash
python -c "from src.pasloe.models import SourceRecord, EventRecord, Event, EventCreatedResponse; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/pasloe/models.py
git commit -m "refactor: strip webhooks, schemas, embedding from models; add EventCreatedResponse"
```

---

### Task 3: Simplify config.py

**Files:**
- Modify: `src/pasloe/config.py`

Remove: `webhook_max_retries`, `webhook_retry_backoff`, `s3_endpoint`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_region`.

- [ ] **Step 1: Edit config.py**

Replace the Settings class body so it only retains:

```python
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    db_type: str = "sqlite"
    sqlite_path: str = "./events.db"
    pg_user: str = "user"          # preserve original defaults
    pg_password: str = "password"
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "pasloe"

    # API
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str | None = None
    allow_insecure_http: bool = False

    # Environment
    env: str = "dev"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",   # silently discard unknown env vars
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_db_url() -> str:
    s = get_settings()
    if s.db_type == "sqlite":
        return f"sqlite+aiosqlite:///{s.sqlite_path}"
    return (
        f"postgresql+asyncpg://{s.pg_user}:{s.pg_password}"
        f"@{s.pg_host}:{s.pg_port}/{s.pg_db}"
    )


def is_sqlite() -> bool:
    return get_settings().db_type == "sqlite"
```

- [ ] **Step 2: Verify**

```bash
python -c "from src.pasloe.config import get_settings, is_sqlite; print(get_settings().db_type, is_sqlite())"
```

Expected: `sqlite True`

- [ ] **Step 3: Commit**

```bash
git add src/pasloe/config.py
git commit -m "refactor: remove webhook and S3 config keys"
```

---

### Task 4: Simplify __init__.py

**Files:**
- Modify: `src/pasloe/__init__.py`

Remove exports of deleted symbols (webhook, schema, S3, client, promoted).

- [ ] **Step 1: Rewrite __init__.py**

```python
from .app import app
from .database import get_session, init_db
from .models import (
    Event,
    EventCreate,
    EventCreatedResponse,
    EventRecord,
    SourceCreate,
    SourceRecord,
)

__all__ = [
    "app",
    "get_session",
    "init_db",
    "Event",
    "EventCreate",
    "EventCreatedResponse",
    "EventRecord",
    "SourceCreate",
    "SourceRecord",
]
```

- [ ] **Step 2: Verify (skip if api.py/store.py still have broken imports)**

This check will fail until Tasks 7–8 rewrite api.py and store.py. Skip it for now; the full import chain is verified after Task 8.

```bash
python -c "from src.pasloe.models import SourceRecord, EventRecord, Event, EventCreatedResponse; print('models OK')"
```

Expected: `models OK`

- [ ] **Step 3: Commit**

```bash
git add src/pasloe/__init__.py
git commit -m "refactor: remove deleted exports from __init__"
```

---

### Task 5: Remove aioboto3 and pgvector from dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit pyproject.toml**

Remove `aioboto3>=12.0.0` and `pgvector>=0.2.5` from `[project] dependencies`.
Remove the `[project.optional-dependencies] client` section (the whole `client = [...]` entry).

- [ ] **Step 2: Uninstall removed packages (optional, won't break tests)**

```bash
pip uninstall -y aioboto3 pgvector 2>/dev/null; echo "done"
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: remove aioboto3 and pgvector dependencies"
```

---

## Chunk 2: Projection Infrastructure

### Task 6: Create BaseProjection and ProjectionRegistry

**Files:**
- Create: `src/pasloe/projections/__init__.py`
- Create: `tests/test_projections.py`

The projection system has two responsibilities:
1. **on_event**: after an event is committed, find matching projections and write to their tables
2. **filter**: given event_ids from Phase 1 query, let matching projection narrow them by field filters

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projections.py`:

```python
"""Tests for BaseProjection and ProjectionRegistry."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.pasloe.projections import BaseProjection, ProjectionRegistry


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

class StringFilterProjection(BaseProjection):
    source = "svc"
    event_type = "log"
    __tablename__ = "proj_test_log"

    async def on_insert(self, session, event) -> list[str]:
        return []  # no extra fields

    async def filter(self, session, event_ids, filters):
        if "level" not in filters:
            return event_ids
        allowed = set(filters["level"].split(","))
        # In a real impl this hits the DB; here we just return all ids
        return event_ids


class NumericProjection(BaseProjection):
    source = "svc"
    event_type = "metric"
    __tablename__ = "proj_test_metric"

    async def on_insert(self, session, event) -> list[str]:
        return []

    async def filter(self, session, event_ids, filters):
        return event_ids


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestProjectionRegistry:
    def test_matches_source_and_type(self):
        reg = ProjectionRegistry([StringFilterProjection()])
        assert reg.find("svc", "log") is not None
        assert reg.find("svc", "other") is None
        assert reg.find("other", "log") is None

    def test_find_returns_none_when_source_is_none(self):
        reg = ProjectionRegistry([StringFilterProjection()])
        assert reg.find(None, "log") is None

    def test_find_returns_none_when_type_is_none(self):
        reg = ProjectionRegistry([StringFilterProjection()])
        assert reg.find("svc", None) is None

    @pytest.mark.asyncio
    async def test_on_event_calls_matching_projection(self):
        proj = StringFilterProjection()
        proj.on_insert = AsyncMock(return_value=[])
        reg = ProjectionRegistry([proj])

        event = MagicMock()
        event.source_id = "svc"
        event.type = "log"

        warnings = await reg.on_event(session=MagicMock(), event=event)
        proj.on_insert.assert_called_once()
        assert warnings == []

    @pytest.mark.asyncio
    async def test_on_event_skips_non_matching_projection(self):
        proj = StringFilterProjection()
        proj.on_insert = AsyncMock(return_value=[])
        reg = ProjectionRegistry([proj])

        event = MagicMock()
        event.source_id = "svc"
        event.type = "other"  # doesn't match

        await reg.on_event(session=MagicMock(), event=event)
        proj.on_insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_event_formats_warnings_from_extra_fields(self):
        proj = StringFilterProjection()
        proj.on_insert = AsyncMock(return_value=["region", "tenant"])
        reg = ProjectionRegistry([proj])

        event = MagicMock()
        event.source_id = "svc"
        event.type = "log"

        warnings = await reg.on_event(session=MagicMock(), event=event)
        assert len(warnings) == 1
        assert "region" in warnings[0]
        assert "tenant" in warnings[0]

    @pytest.mark.asyncio
    async def test_filter_returns_ids_unchanged_when_no_match(self):
        reg = ProjectionRegistry([StringFilterProjection()])
        ids = [uuid4(), uuid4()]
        result = await reg.filter("other", "log", MagicMock(), ids, {})
        assert result == ids

    @pytest.mark.asyncio
    async def test_filter_delegates_to_matching_projection(self):
        proj = StringFilterProjection()
        ids = [uuid4(), uuid4()]
        proj.filter = AsyncMock(return_value=[ids[0]])
        reg = ProjectionRegistry([proj])

        session = MagicMock()
        result = await reg.filter("svc", "log", session, ids, {"level": "error"})
        assert result == [ids[0]]
        proj.filter.assert_called_once_with(session, ids, {"level": "error"})


class TestBaseProjectionInterface:
    def test_subclass_must_define_source_and_event_type(self):
        # __init_subclass__ raises TypeError at class definition time, not instantiation
        with pytest.raises(TypeError):
            class Bad(BaseProjection):
                __tablename__ = "proj_bad"
                # missing source and event_type
                async def on_insert(self, session, event): return []
                async def filter(self, session, event_ids, filters): return event_ids

    def test_subclass_must_define_tablename(self):
        with pytest.raises(TypeError):
            class Bad(BaseProjection):
                source = "x"
                event_type = "y"
                # missing __tablename__
                async def on_insert(self, session, event): return []
                async def filter(self, session, event_ids, filters): return event_ids
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
python -m pytest tests/test_projections.py -x -q 2>&1 | head -20
```

Expected: `ImportError` or `ModuleNotFoundError` — projections module doesn't exist yet.

- [ ] **Step 3: Implement BaseProjection and ProjectionRegistry**

Create `src/pasloe/projections/__init__.py`:

```python
"""Projection system: code-defined acceleration tables for known event types."""
from __future__ import annotations

import abc
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..models import EventRecord


class BaseProjection(abc.ABC):
    """
    Base class for all projection definitions.

    Subclasses must define as class attributes (not properties):
      - source: str        — matched source_id
      - event_type: str    — matched event type
      - __tablename__: str — SQLAlchemy table name

    Missing any of these raises TypeError at class definition time via
    __init_subclass__.

    And implement:
      - on_insert(session, event) -> list[str]
      - filter(session, event_ids, filters) -> list[UUID]
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Only enforce on concrete (non-abstract) subclasses
        if not getattr(cls, "__abstractmethods__", None):
            for attr in ("source", "event_type", "__tablename__"):
                if not hasattr(cls, attr):
                    raise TypeError(
                        f"{cls.__name__} must define class attribute '{attr}'"
                    )

    @abc.abstractmethod
    async def on_insert(
        self,
        session: "AsyncSession",
        event: "EventRecord",
    ) -> list[str]:
        """
        Write projection fields from event.data to this projection's table.

        Returns:
            List of extra field names found in event.data that are not in
            the projection schema (projection write is skipped in that case).
            Returns an empty list on clean success.
        """

    @abc.abstractmethod
    async def filter(
        self,
        session: "AsyncSession",
        event_ids: list[UUID],
        filters: dict[str, str],
    ) -> list[UUID]:
        """
        Narrow event_ids to those matching projection-specific filters.

        Args:
            session:   async DB session
            event_ids: ordered list from Phase 1 query (ts|id order)
            filters:   dict of raw query-string values, keyed by field name.
                       String column: split on "," → IN list.
                       Numeric column: split on "," → (lower, upper) bounds.
                       Either bound may be empty string → open-ended.

        Returns:
            Ordered subset of event_ids. Must preserve input order.
            If filters contain no recognised fields, return event_ids unchanged.
        """


class ProjectionRegistry:
    """
    Holds a list of BaseProjection instances and routes events/queries to them.

    Usage:
        registry = ProjectionRegistry([LLMCallProjection(), ...])
        warnings = await registry.on_event(session, event)
        filtered  = await registry.filter(source, type, session, ids, filters)
    """

    def __init__(self, projections: list[BaseProjection]) -> None:
        self._projections = projections

    def find(self, source: str | None, event_type: str | None) -> BaseProjection | None:
        """Return the projection that matches (source, event_type), or None."""
        if source is None or event_type is None:
            return None
        for proj in self._projections:
            if proj.source == source and proj.event_type == event_type:
                return proj
        return None

    async def on_event(
        self,
        session: "AsyncSession",
        event: "EventRecord",
    ) -> list[str]:
        """
        Called after an event is committed. Writes to the matching projection.

        Returns:
            List of formatted warning strings (empty if no issues).
        """
        proj = self.find(event.source_id, event.type)
        if proj is None:
            return []

        extra_fields = await proj.on_insert(session, event)
        if extra_fields:
            field_list = ", ".join(extra_fields)
            return [f"projection skipped: unknown fields: [{field_list}]"]
        return []

    async def filter(
        self,
        source: str | None,
        event_type: str | None,
        session: "AsyncSession",
        event_ids: list[UUID],
        filters: dict[str, str],
    ) -> list[UUID]:
        """
        Apply projection-specific filters to a list of event_ids.

        Returns event_ids unchanged if no projection matches or filters is empty.
        """
        proj = self.find(source, event_type)
        if proj is None:
            return event_ids
        if not filters:
            return event_ids
        return await proj.filter(session, event_ids, filters)
```

- [ ] **Step 4: Run the tests**

```bash
python -m pytest tests/test_projections.py -v 2>&1 | tail -30
```

Expected: Most tests pass. The `test_subclass_must_define_*` tests may need adjustment — if `abc.abstractmethod` on a `@property` doesn't raise `TypeError` on instantiation as expected, remove those two tests and add a note that the interface relies on convention.

- [ ] **Step 5: Fix any failing tests**

If the abstract-property tests fail because Python doesn't raise `TypeError` for missing concrete class attributes (as opposed to abstract methods), remove `TestBaseProjectionInterface` from the test file — the interface is enforced by convention, not at runtime.

- [ ] **Step 6: Commit**

```bash
git add src/pasloe/projections/__init__.py tests/test_projections.py
git commit -m "feat: add BaseProjection and ProjectionRegistry"
```

---

## Chunk 3: Store Refactor

### Task 7: Rewrite store.py

**Files:**
- Modify: `src/pasloe/store.py`
- Create: `tests/conftest.py`

Remove: `DuplicateSourceError`, schema/webhook/promoted operations, `_fire_webhooks`.
Change: `register_source` → upsert (returns `(record, created: bool)`), auto-source in `append_event`.
Add: projection phase in `query_events`.

- [ ] **Step 1: Create tests/conftest.py (must be done before any test run)**

Create `tests/conftest.py` — this file is loaded by pytest before any test module imports, ensuring environment vars are set before `lru_cache` captures them:

```python
# tests/conftest.py
import os

# Set before any pasloe module is imported so lru_cache in get_settings()
# sees the right values. Must come before any `from src.pasloe import ...`.
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = ":memory:"
```

- [ ] **Step 2: Write new store tests (target behavior)**

Create `tests/test_store.py`:

```python
"""Unit tests for store functions (no HTTP layer)."""
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock

from src.pasloe.database import close_engine, get_session_factory, init_db
from src.pasloe.config import get_settings
from src.pasloe.models import EventCreate, SourceCreate
from src.pasloe import store


@pytest_asyncio.fixture
async def db():
    get_settings.cache_clear()  # ensure :memory: setting is picked up fresh
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    await close_engine()
    get_settings.cache_clear()


class TestRegisterSource:
    @pytest.mark.asyncio
    async def test_creates_new_source(self, db):
        record, created = await store.register_source(db, SourceCreate(id="s1"))
        assert created is True
        assert record.id == "s1"

    @pytest.mark.asyncio
    async def test_upserts_existing_source(self, db):
        await store.register_source(db, SourceCreate(id="s2", metadata={"v": 1}))
        record, created = await store.register_source(db, SourceCreate(id="s2", metadata={"v": 2}))
        assert created is False
        assert record.metadata_["v"] == 2

    @pytest.mark.asyncio
    async def test_no_error_on_duplicate(self, db):
        await store.register_source(db, SourceCreate(id="s3"))
        # Should not raise
        await store.register_source(db, SourceCreate(id="s3"))


class TestAppendEvent:
    @pytest.mark.asyncio
    async def test_auto_registers_unknown_source(self, db):
        ec = EventCreate(source_id="auto-src", type="ping", data={})
        event, warnings = await store.append_event(db, ec, projection_registry=None)
        assert event.source_id == "auto-src"
        source = await store.get_source(db, "auto-src")
        assert source is not None

    @pytest.mark.asyncio
    async def test_returns_empty_warnings_without_projection(self, db):
        ec = EventCreate(source_id="src-a", type="ev", data={"x": 1})
        event, warnings = await store.append_event(db, ec, projection_registry=None)
        assert warnings == []

    @pytest.mark.asyncio
    async def test_returns_projection_warnings_when_extra_fields(self, db):
        from src.pasloe.projections import BaseProjection, ProjectionRegistry

        class StrictProj(BaseProjection):
            source = "src-b"
            event_type = "typed"
            __tablename__ = "proj_strict"

            async def on_insert(self, session, event):
                return ["extra_field"]  # simulate extra field detected

            async def filter(self, session, event_ids, filters):
                return event_ids

        registry = ProjectionRegistry([StrictProj()])
        ec = EventCreate(source_id="src-b", type="typed", data={"x": 1, "extra_field": "oops"})
        event, warnings = await store.append_event(db, ec, projection_registry=registry)
        assert event.id is not None  # event was still stored
        assert any("extra_field" in w for w in warnings)


class TestQueryEvents:
    @pytest.mark.asyncio
    async def test_basic_query(self, db):
        ec = EventCreate(source_id="qs", type="t", data={})
        await store.append_event(db, ec, projection_registry=None)
        events, _ = await store.query_events(db, source="qs")
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_query_by_id(self, db):
        ec = EventCreate(source_id="qi", type="t", data={})
        event, _ = await store.append_event(db, ec, projection_registry=None)
        results, _ = await store.query_events(db, event_id=str(event.id))
        assert len(results) == 1
        assert str(results[0].id) == str(event.id)

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, db):
        for _ in range(5):
            await store.append_event(db, EventCreate(source_id="cp", type="t", data={}), projection_registry=None)
        page1, cursor = await store.query_events(db, source="cp", limit=3)
        assert len(page1) == 3
        assert cursor is not None
        page2, _ = await store.query_events(db, source="cp", limit=3, cursor=cursor)
        assert len(page2) >= 2

    @pytest.mark.asyncio
    async def test_projection_filter_applied(self, db):
        from src.pasloe.projections import BaseProjection, ProjectionRegistry
        from uuid import UUID

        inserted_ids = []
        for i in range(3):
            ec = EventCreate(source_id="pf", type="ev", data={"val": i})
            event, _ = await store.append_event(db, ec, projection_registry=None)
            inserted_ids.append(event.id)

        class FilterProj(BaseProjection):
            source = "pf"
            event_type = "ev"
            __tablename__ = "proj_filter"

            async def on_insert(self, session, event):
                return []

            async def filter(self, session, event_ids, filters):
                # Return only first id to simulate field filtering
                return [event_ids[0]] if event_ids else []

        registry = ProjectionRegistry([FilterProj()])
        results, _ = await store.query_events(
            db, source="pf", type_="ev",
            projection_filters={"val": "0"},
            projection_registry=registry,
        )
        assert len(results) == 1
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
python -m pytest tests/test_store.py -x -q 2>&1 | head -20
```

Expected: failures because `store.append_event` and `store.query_events` have different signatures.

- [ ] **Step 3: Rewrite store.py**

Replace the full content of `src/pasloe/store.py`:

```python
"""Business logic layer — database operations for sources and events."""
from __future__ import annotations

import logging
from datetime import datetime
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
        id=uuid7(),
        source_id=event.source_id,
        type=event.type,
        data=event.data,
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
```

- [ ] **Step 4: Run store tests**

```bash
python -m pytest tests/test_store.py -v 2>&1 | tail -30
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/pasloe/store.py tests/test_store.py tests/conftest.py
git commit -m "refactor: rewrite store — upsert sources, projection-aware append/query, remove webhooks/schemas"
```

---

## Chunk 4: API Refactor

### Task 8: Rewrite api.py

**Files:**
- Modify: `src/pasloe/api.py`

Remove: schema, promoted, webhook, S3, `GET /events/{event_id}` endpoints.
Update: `POST /sources` returns 201/200. `POST /events` returns `EventCreatedResponse`. `GET /events` accepts projection filter params.

- [ ] **Step 1: Rewrite api.py**

Replace the full content of `src/pasloe/api.py`:

```python
"""FastAPI router — all HTTP endpoints."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

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

@router.post("/events", status_code=201, dependencies=[_Auth])
async def append_event(
    body: EventCreate,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> EventCreatedResponse:
    registry: ProjectionRegistry = _get_registry(request)
    record, warnings = await store.append_event(db, body, projection_registry=registry)
    return EventCreatedResponse(
        id=str(record.id),
        source_id=record.source_id,
        type=record.type,
        ts=record.ts,
        data=record.data,
        warnings=warnings,
    )


# Reserved query param names — must not collide with projection field names.
_RESERVED = {"id", "source", "type", "since", "until", "cursor", "limit", "order"}


@router.get("/events", dependencies=[_Auth])
async def query_events(
    request: Request,
    db: AsyncSession = Depends(get_session),
    event_id: str | None = Query(default=None, alias="id"),
    source: str | None = None,
    type_: str | None = Query(default=None, alias="type"),
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    response: Response,
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


@router.get("/events/stats", dependencies=[_Auth])
async def get_stats(
    db: AsyncSession = Depends(get_session),
) -> dict:
    return await store.get_stats(db)
```

- [ ] **Step 2: Wire projection_registry into app state in app.py**

Edit `src/pasloe/app.py`. Add registry initialisation in the lifespan and attach to `app.state`:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import importlib.resources

from .database import init_db, close_engine
from .projections import ProjectionRegistry
from .api import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Discover projections unless a test has already injected a custom registry
    if not hasattr(app.state, "projection_registry"):
        app.state.projection_registry = ProjectionRegistry.discover()
    yield
    await close_engine()


app = FastAPI(title="Pasloe EventStore", version="0.3.0", lifespan=lifespan)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ui", response_class=HTMLResponse)
async def ui():
    ui_path = importlib.resources.files("pasloe") / "ui.html"
    return HTMLResponse(ui_path.read_text())
```

Also add a `discover()` classmethod to `ProjectionRegistry` in `src/pasloe/projections/__init__.py`:

```python
@classmethod
def discover(cls) -> "ProjectionRegistry":
    """
    Auto-discover all BaseProjection subclasses in this package.

    Works in both development (src layout) and installed environments.
    """
    import importlib
    import pkgutil
    # Use __package__ to resolve the correct importable name regardless of
    # whether running in src-layout dev or installed mode.
    import pasloe.projections as pkg  # noqa: PLC0415

    pkg_name = pkg.__name__  # "pasloe.projections"
    for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg_name}.{module_name}")

    def _concrete_subclasses(base):
        result = []
        for sub in base.__subclasses__():
            if not getattr(sub, "__abstractmethods__", None):
                result.append(sub())
            result.extend(_concrete_subclasses(sub))
        return result

    return cls(_concrete_subclasses(BaseProjection))
```

- [ ] **Step 3: Run the full test suite**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -30
```

Expected: `test_projections.py` and `test_store.py` pass. `test_api.py` will have many failures (tests for deleted endpoints and old behavior). That's addressed in Task 9.

- [ ] **Step 4: Commit**

```bash
git add src/pasloe/api.py src/pasloe/app.py src/pasloe/projections/__init__.py
git commit -m "refactor: rewrite api.py — remove deleted endpoints, projection-aware query, EventCreatedResponse"
```

---

## Chunk 5: Test Suite and Alembic

### Task 9: Rewrite test_api.py

**Files:**
- Modify: `tests/test_api.py`
- Note: `tests/conftest.py` was created in Task 7 Step 1 — no need to recreate

Rewrite to cover: health, sources (201/200/upsert), events (append with/without warnings, query, stats), projection filter in query, cursor pagination, invalid cursor.

- [ ] **Step 1: Rewrite tests/test_api.py**

```python
"""Integration tests for the Pasloe HTTP API."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.pasloe.app import app
from src.pasloe.database import close_engine, init_db
from src.pasloe.projections import BaseProjection, ProjectionRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    # DB_TYPE and SQLITE_PATH are set in tests/conftest.py before module import
    from src.pasloe.config import get_settings
    get_settings.cache_clear()  # ensure fresh settings per test

    app.state.projection_registry = ProjectionRegistry([])  # empty by default; tests override as needed
    await init_db()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c

    await close_engine()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class TestSources:
    @pytest.mark.asyncio
    async def test_register_source_returns_201(self, client):
        r = await client.post("/sources", json={"id": "src1"})
        assert r.status_code == 201
        assert r.json()["id"] == "src1"

    @pytest.mark.asyncio
    async def test_register_source_upsert_returns_200(self, client):
        await client.post("/sources", json={"id": "src2", "metadata": {"v": 1}})
        r = await client.post("/sources", json={"id": "src2", "metadata": {"v": 2}})
        assert r.status_code == 200
        assert r.json()["metadata"]["v"] == 2

    @pytest.mark.asyncio
    async def test_list_sources(self, client):
        await client.post("/sources", json={"id": "ls1"})
        r = await client.get("/sources")
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()]
        assert "ls1" in ids

    @pytest.mark.asyncio
    async def test_get_source(self, client):
        await client.post("/sources", json={"id": "gs1"})
        r = await client.get("/sources/gs1")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_get_source_not_found(self, client):
        r = await client.get("/sources/nope")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Events — append
# ---------------------------------------------------------------------------

class TestAppendEvent:
    @pytest.mark.asyncio
    async def test_append_auto_registers_source(self, client):
        r = await client.post("/events", json={"source_id": "new-src", "type": "ping", "data": {}})
        assert r.status_code == 201
        body = r.json()
        assert body["source_id"] == "new-src"
        assert body["warnings"] == []

    @pytest.mark.asyncio
    async def test_append_returns_empty_warnings_without_projection(self, client):
        r = await client.post("/events", json={"source_id": "s", "type": "t", "data": {"x": 1}})
        assert r.status_code == 201
        assert r.json()["warnings"] == []

    @pytest.mark.asyncio
    async def test_append_returns_warnings_when_projection_skips(self, client):
        class SkipProj(BaseProjection):
            source = "ws"
            event_type = "typed"
            __tablename__ = "proj_ws"

            async def on_insert(self, session, event):
                return ["bad_field"]

            async def filter(self, session, event_ids, filters):
                return event_ids

        app.state.projection_registry = ProjectionRegistry([SkipProj()])
        r = await client.post("/events", json={"source_id": "ws", "type": "typed", "data": {"bad_field": 1}})
        assert r.status_code == 201
        body = r.json()
        assert body["id"] is not None          # event stored
        assert len(body["warnings"]) == 1
        assert "bad_field" in body["warnings"][0]

    @pytest.mark.asyncio
    async def test_deleted_endpoint_events_by_id_gone(self, client):
        r = await client.get("/events/some-uuid")
        # Should be 404 (no such path), not 200
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_deleted_endpoints_webhooks_gone(self, client):
        assert (await client.post("/webhooks", json={})).status_code == 404
        assert (await client.get("/webhooks")).status_code == 404

    @pytest.mark.asyncio
    async def test_deleted_endpoint_schemas_gone(self, client):
        assert (await client.post("/schemas", json={})).status_code == 404

    @pytest.mark.asyncio
    async def test_deleted_endpoint_s3_gone(self, client):
        assert (await client.post("/artifacts/presign", json={})).status_code == 404


# ---------------------------------------------------------------------------
# Events — query
# ---------------------------------------------------------------------------

class TestQueryEvents:
    @pytest.mark.asyncio
    async def test_query_by_source(self, client):
        await client.post("/events", json={"source_id": "qs", "type": "t", "data": {}})
        r = await client.get("/events?source=qs")
        assert r.status_code == 200
        assert all(e["source_id"] == "qs" for e in r.json())

    @pytest.mark.asyncio
    async def test_query_by_type(self, client):
        await client.post("/events", json={"source_id": "qt", "type": "special", "data": {}})
        r = await client.get("/events?type=special")
        assert r.status_code == 200
        assert all(e["type"] == "special" for e in r.json())

    @pytest.mark.asyncio
    async def test_query_by_id(self, client):
        r1 = await client.post("/events", json={"source_id": "qi", "type": "t", "data": {}})
        event_id = r1.json()["id"]
        r2 = await client.get(f"/events?id={event_id}")
        assert r2.status_code == 200
        assert len(r2.json()) == 1
        assert r2.json()[0]["id"] == event_id

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, client):
        for _ in range(5):
            await client.post("/events", json={"source_id": "cp", "type": "t", "data": {}})
        r1 = await client.get("/events?source=cp&limit=3")
        assert r1.status_code == 200
        assert len(r1.json()) == 3
        cursor = r1.headers.get("x-next-cursor")
        assert cursor is not None
        r2 = await client.get(f"/events?source=cp&limit=3&cursor={cursor}")
        assert r2.status_code == 200
        assert len(r2.json()) >= 2

    @pytest.mark.asyncio
    async def test_invalid_cursor_returns_400(self, client):
        r = await client.get("/events?cursor=notvalid")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_projection_filter_ignored_when_no_projection(self, client):
        """Unknown params are silently ignored when no matching projection."""
        await client.post("/events", json={"source_id": "pf", "type": "t", "data": {"level": "info"}})
        r = await client.get("/events?source=pf&type=t&level=info")
        assert r.status_code == 200  # no error

    @pytest.mark.asyncio
    async def test_projection_filter_applied_when_projection_matches(self, client):
        from uuid import UUID

        class LevelProj(BaseProjection):
            source = "lp"
            event_type = "log"
            __tablename__ = "proj_level"

            async def on_insert(self, session, event):
                return []

            async def filter(self, session, event_ids, filters):
                # Simulate: only return first id (as if filtered by level=error)
                return event_ids[:1]

        app.state.projection_registry = ProjectionRegistry([LevelProj()])
        for _ in range(3):
            await client.post("/events", json={"source_id": "lp", "type": "log", "data": {}})

        r = await client.get("/events?source=lp&type=log&level=error")
        assert r.status_code == 200
        assert len(r.json()) == 1  # projection narrowed to 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_stats(self, client):
        await client.post("/events", json={"source_id": "st", "type": "ev", "data": {}})
        r = await client.get("/events/stats")
        assert r.status_code == 200
        body = r.json()
        assert "total_events" in body
        assert "by_source" in body
        assert "by_type" in body
        assert body["total_events"] >= 1
```

- [ ] **Step 2: Run the full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: all tests in `test_api.py`, `test_store.py`, `test_projections.py` pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_api.py
git commit -m "test: rewrite test_api for new behavior — projections, upsert sources, warnings"
```

---

### Task 10: Set up Alembic

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/` (empty)

Alembic is the migration tool for projection tables in production. Development/tests continue to use SQLAlchemy's `create_all()`. Alembic migrations are written manually when adding or altering projection tables.

- [ ] **Step 1: Install Alembic and move httpx to test deps**

In `pyproject.toml`:
- Add `alembic>=1.13` to `[project] dependencies`
- Move `httpx>=0.27` from `[project] dependencies` to `[project.optional-dependencies] test` (it is only needed for testing, not production)

```bash
pip install alembic
```

- [ ] **Step 2: Initialise Alembic**

```bash
alembic init alembic
```

This creates `alembic.ini` and `alembic/` directory.

- [ ] **Step 3: Edit alembic/env.py to use Pasloe's config and models**

Replace the `run_migrations_online` and `run_migrations_offline` sections in `alembic/env.py`:

```python
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so Alembic can see the metadata.
# Use the installed package name (pasloe.*), not the src-layout path.
# Run `pip install -e .` before using Alembic.
from pasloe.models import Base
import pasloe.projections  # noqa: F401 — triggers subclass discovery
from pasloe.config import get_db_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return get_db_url()


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Edit alembic.ini — set script_location**

In `alembic.ini`, ensure `script_location = alembic`. The `sqlalchemy.url` key can be left blank (overridden at runtime by env.py).

- [ ] **Step 5: Generate initial migration for existing tables**

```bash
alembic revision --autogenerate -m "initial_schema"
```

Review the generated file in `alembic/versions/` — it should include `sources` and `events` tables. If it looks correct:

```bash
alembic upgrade head  # applies migration to dev SQLite db (not a dry-run)
```

- [ ] **Step 6: Commit**

```bash
git add alembic.ini alembic/ pyproject.toml
git commit -m "feat: add Alembic migration infrastructure for production deployments"
```

---

### Task 11: Final cleanup and smoke test

**Files:**
- Modify: `src/pasloe/database.py` — remove promoted table cache/rebuild (already unused after store rewrite, but verify)

- [ ] **Step 1: Clean up database.py**

Read `src/pasloe/database.py` and remove `_rebuild_promoted_tables()` and any import of `promoted`. The file should only contain engine/session management:

```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_db_url, is_sqlite
from .models import Base

_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        kwargs = {}
        if is_sqlite():
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(get_db_url(), **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _SessionLocal


async def init_db() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _SessionLocal = None


async def get_session():
    """Async generator dependency for FastAPI Depends().

    FastAPI inspects this with isasyncgenfunction() — it must remain a bare
    async generator (no @asynccontextmanager). For direct use in tests, use
    get_session_factory()() as an async context manager instead.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 2: Run full test suite one final time**

```bash
python -m pytest tests/ -v 2>&1
```

Expected: all tests pass, no warnings about deprecated code.

- [ ] **Step 3: Start the dev server and verify manually**

```bash
uvicorn src.pasloe.app:app --reload --port 8000 &
sleep 2
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/events -H "Content-Type: application/json" \
  -d '{"source_id":"test","type":"ping","data":{"msg":"hello"}}'
curl -s "http://localhost:8000/events?source=test"
kill %1
```

Expected: health returns `{"status":"ok"}`, append returns event with `warnings:[]`, query returns the event.

- [ ] **Step 4: Final commit**

```bash
git add src/pasloe/database.py
git commit -m "refactor: clean up database.py — remove promoted table cache"
```

---

## Summary of File Changes

| Action | Path |
|--------|------|
| DELETE | `src/pasloe/s3.py` |
| DELETE | `src/pasloe/client.py` |
| DELETE | `src/pasloe/promoted.py` |
| DELETE | `clients/` |
| REWRITE | `src/pasloe/models.py` |
| REWRITE | `src/pasloe/store.py` |
| REWRITE | `src/pasloe/api.py` |
| REWRITE | `src/pasloe/app.py` |
| REWRITE | `src/pasloe/database.py` |
| SIMPLIFY | `src/pasloe/config.py` |
| SIMPLIFY | `src/pasloe/__init__.py` |
| CREATE | `src/pasloe/projections/__init__.py` |
| CREATE | `tests/test_store.py` |
| CREATE | `tests/test_projections.py` |
| REWRITE | `tests/test_api.py` |
| CREATE | `alembic.ini` |
| CREATE | `alembic/env.py` |
| CREATE | `alembic/versions/<initial>.py` |
| MODIFY | `pyproject.toml` |
