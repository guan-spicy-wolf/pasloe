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
