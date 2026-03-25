"""Integration tests for the Pasloe HTTP API."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.pasloe.app import app
from src.pasloe.database import close_engine, get_session_factory, init_db
from src.pasloe.pipeline import PipelineConfig, PipelineRuntime
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
    pipeline = PipelineRuntime(
        session_factory=get_session_factory(),
        projection_registry=app.state.projection_registry,
        config=PipelineConfig(
            poll_interval_seconds=0.01,
            batch_size=64,
            lease_seconds=5,
            retry_base_seconds=0.05,
            retry_max_seconds=1.0,
        ),
    )
    await pipeline.start()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c

    await pipeline.stop()
    await close_engine()
    get_settings.cache_clear()


async def _wait_event_visible(client, event_id: str, timeout_s: float = 2.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get(f"/events?id={event_id}")
        if r.status_code == 200 and r.json():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"event {event_id} did not become visible within {timeout_s}s")


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
        assert r.status_code == 202
        body = r.json()
        assert body["source_id"] == "new-src"
        assert body["warnings"] == []
        assert body["status"] == "accepted"
        await _wait_event_visible(client, body["id"])

    @pytest.mark.asyncio
    async def test_append_returns_empty_warnings_without_projection(self, client):
        r = await client.post("/events", json={"source_id": "s", "type": "t", "data": {"x": 1}})
        assert r.status_code == 202
        assert r.json()["warnings"] == []
        await _wait_event_visible(client, r.json()["id"])

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
        assert r.status_code == 202
        body = r.json()
        assert body["id"] is not None
        assert body["warnings"] == []
        await _wait_event_visible(client, body["id"])

    @pytest.mark.asyncio
    async def test_deleted_endpoint_events_by_id_gone(self, client):
        r = await client.get("/events/some-uuid")
        # Should be 404 (no such path), not 200
        assert r.status_code == 404

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
        r0 = await client.post("/events", json={"source_id": "qs", "type": "t", "data": {}})
        await _wait_event_visible(client, r0.json()["id"])
        r = await client.get("/events?source=qs")
        assert r.status_code == 200
        assert all(e["source_id"] == "qs" for e in r.json())

    @pytest.mark.asyncio
    async def test_query_by_type(self, client):
        r0 = await client.post("/events", json={"source_id": "qt", "type": "special", "data": {}})
        await _wait_event_visible(client, r0.json()["id"])
        r = await client.get("/events?type=special")
        assert r.status_code == 200
        assert all(e["type"] == "special" for e in r.json())

    @pytest.mark.asyncio
    async def test_query_by_id(self, client):
        r1 = await client.post("/events", json={"source_id": "qi", "type": "t", "data": {}})
        event_id = r1.json()["id"]
        await _wait_event_visible(client, event_id)
        r2 = await client.get(f"/events?id={event_id}")
        assert r2.status_code == 200
        assert len(r2.json()) == 1
        assert r2.json()[0]["id"] == event_id

    @pytest.mark.asyncio
    async def test_invalid_cursor_returns_400(self, client):
        r = await client.get("/events?cursor=notvalid")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_projection_filter_ignored_when_no_projection(self, client):
        """Unknown params are silently ignored when no matching projection."""
        r0 = await client.post("/events", json={"source_id": "pf", "type": "t", "data": {"level": "info"}})
        await _wait_event_visible(client, r0.json()["id"])
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
            r0 = await client.post("/events", json={"source_id": "lp", "type": "log", "data": {}})
            await _wait_event_visible(client, r0.json()["id"])

        r = await client.get("/events?source=lp&type=log&level=error")
        assert r.status_code == 200
        assert len(r.json()) == 1  # projection narrowed to 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_stats(self, client):
        r0 = await client.post("/events", json={"source_id": "st", "type": "ev", "data": {}})
        await _wait_event_visible(client, r0.json()["id"])
        r = await client.get("/events/stats")
        assert r.status_code == 200
        body = r.json()
        assert "total_events" in body
        assert "by_source" in body
        assert "by_type" in body
        assert body["total_events"] >= 1


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

class TestWebhooks:
    @pytest.mark.asyncio
    async def test_register_webhook(self, client):
        r = await client.post("/webhooks", json={"url": "http://test.host/h"})
        assert r.status_code == 201
        data = r.json()
        assert data["url"] == "http://test.host/h"
        assert "id" in data
        assert "has_secret" in data  # secret is not exposed directly

    @pytest.mark.asyncio
    async def test_register_webhook_idempotent(self, client):
        r1 = await client.post("/webhooks", json={"url": "http://x.test/h", "secret": "a"})
        r2 = await client.post("/webhooks", json={"url": "http://x.test/h", "secret": "b"})
        assert r1.status_code == 201
        assert r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.json()["has_secret"] is True  # secret "b" is set

    @pytest.mark.asyncio
    async def test_list_webhooks(self, client):
        await client.post("/webhooks", json={"url": "http://list.test/h"})
        r = await client.get("/webhooks")
        assert r.status_code == 200
        assert any(w["url"] == "http://list.test/h" for w in r.json())

    @pytest.mark.asyncio
    async def test_delete_webhook(self, client):
        r = await client.post("/webhooks", json={"url": "http://del.test/h"})
        wh_id = r.json()["id"]
        r2 = await client.delete(f"/webhooks/{wh_id}")
        assert r2.status_code == 204
        r3 = await client.get("/webhooks")
        assert not any(w["id"] == wh_id for w in r3.json())

    @pytest.mark.asyncio
    async def test_delete_webhook_not_found(self, client):
        r = await client.delete("/webhooks/nonexistent")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_post_event_triggers_delivery(self, client):
        """Delivery is background — just verify no error on POST /events."""
        await client.post("/sources", json={"id": "src-wh"})
        await client.post("/webhooks", json={"url": "http://nowhere.invalid/h"})
        r = await client.post("/events", json={
            "source_id": "src-wh", "type": "task.submit", "data": {},
        })
        assert r.status_code == 202
