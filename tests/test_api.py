import pytest
  import pytest_asyncio
  from httpx import AsyncClient, ASGITransport

  import os
  os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

  from pasloe.app import app
  from pasloe.database import init_db, close_engine, get_engine


  @pytest_asyncio.fixture
  async def client():
      await init_db()
      async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
          yield c
      await close_engine()


  @pytest.mark.asyncio
  async def test_health(client):
      resp = await client.get("/health")
      assert resp.status_code == 200
      assert resp.json() == {"status": "ok"}


  @pytest.mark.asyncio
  async def test_register_source(client):
      resp = await client.post("/sources", json={
          "id": "metrics",
          "metadata": {"version": "1.0"}
      })
      assert resp.status_code == 201
      data = resp.json()
      assert data["id"] == "metrics"
      assert "kind" not in data


  @pytest.mark.asyncio
  async def test_register_duplicate_source(client):
      payload = {"id": "agent1", "metadata": {}}
      await client.post("/sources", json=payload)
      resp = await client.post("/sources", json=payload)
      assert resp.status_code == 409


  @pytest.mark.asyncio
  async def test_list_sources(client):
      await client.post("/sources", json={"id": "src1", "metadata": {}})
      resp = await client.get("/sources")
      assert resp.status_code == 200
      ids = [s["id"] for s in resp.json()]
      assert "src1" in ids


  @pytest.mark.asyncio
  async def test_append_event(client):
      await client.post("/sources", json={"id": "s1", "metadata": {}})
      resp = await client.post("/events", json={
          "source_id": "s1",
          "type": "startup",
          "data": {"version": "1.0"}
      })
      assert resp.status_code == 201
      data = resp.json()
      assert data["type"] == "startup"
      assert data["source_id"] == "s1"
      assert "session_id" not in data


  @pytest.mark.asyncio
  async def test_append_event_unregistered_source(client):
      resp = await client.post("/events", json={
          "source_id": "ghost",
          "type": "startup",
          "data": {}
      })
      assert resp.status_code == 400


  @pytest.mark.asyncio
  async def test_query_events_by_source(client):
      await client.post("/sources", json={"id": "a1", "metadata": {}})
      await client.post("/events", json={"source_id": "a1", "type": "e1", "data": {}})
      await client.post("/events", json={"source_id": "a1", "type": "e2", "data": {}})

      resp = await client.get("/events?source=a1")
      assert resp.status_code == 200
      events = resp.json()
      assert len(events) == 2
      assert all(e["source_id"] == "a1" for e in events)


  @pytest.mark.asyncio
  async def test_query_events_by_type(client):
      await client.post("/sources", json={"id": "a2", "metadata": {}})
      await client.post("/events", json={"source_id": "a2", "type": "metric", "data": {}})
      await client.post("/events", json={"source_id": "a2", "type": "log", "data": {}})

      resp = await client.get("/events?type=metric")
      assert resp.status_code == 200
      events = resp.json()
      assert all(e["type"] == "metric" for e in events)


  @pytest.mark.asyncio
  async def test_stats(client):
      await client.post("/sources", json={"id": "s2", "metadata": {}})
      await client.post("/events", json={"source_id": "s2", "type": "a", "data": {}})
      await client.post("/events", json={"source_id": "s2", "type": "b", "data": {}})

      resp = await client.get("/events/stats")
      assert resp.status_code == 200
      data = resp.json()
      assert "total_events" in data
      assert data["total_events"] >= 2


  @pytest.mark.asyncio
  async def test_query_events_cursor_pagination(client):
      await client.post("/sources", json={"id": "pager", "metadata": {}})
      await client.post("/events", json={"source_id": "pager", "type": "e1", "data": {}})
      await client.post("/events", json={"source_id": "pager", "type": "e2", "data": {}})
      await client.post("/events", json={"source_id": "pager", "type": "e3", "data": {}})

      first = await client.get("/events", params={"source": "pager", "limit": 2, "order": "asc"})
      assert first.status_code == 200
      assert len(first.json()) == 2
      next_cursor = first.headers.get("x-next-cursor")
      assert next_cursor is not None

      second = await client.get("/events", params={"source": "pager", "limit": 2, "order": "asc", "cursor": next_cursor})
      assert second.status_code == 200
      assert len(second.json()) == 1
      assert second.headers.get("x-next-cursor") is None
      assert [e["type"] for e in first.json() + second.json()] == ["e1", "e2", "e3"]


  @pytest.mark.asyncio
  async def test_query_events_invalid_cursor(client):
      resp = await client.get("/events", params={"cursor": "not-a-cursor"})
      assert resp.status_code == 400


  # --- Schema tests ---

  @pytest.mark.asyncio
  async def test_register_schema(client):
      await client.post("/sources", json={"id": "schsrc", "metadata": {}})
      resp = await client.post("/schemas", json={
          "source_id": "schsrc",
          "type": "llm_response",
          "schema": {
              "model": {"type": "string", "index": True},
              "cost": {"type": "float", "index": False},
              "tokens": {"type": "integer", "index": True},
          }
      })
      assert resp.status_code == 201
      data = resp.json()
      assert data["source_id"] == "schsrc"
      assert data["type"] == "llm_response"
      assert data["end_time"] is None
      assert data["table_name"].startswith("promoted_")


  @pytest.mark.asyncio
  async def test_register_schema_source_not_found(client):
      resp = await client.post("/schemas", json={
          "source_id": "nonexistent",
          "type": "metric",
          "schema": {"value": {"type": "float", "index": False}}
      })
      assert resp.status_code == 400


  @pytest.mark.asyncio
  async def test_schema_driven_data_promotion(client):
      await client.post("/sources", json={"id": "promo", "metadata": {}})
      await client.post("/schemas", json={
          "source_id": "promo",
          "type": "metric",
          "schema": {
              "model": {"type": "string", "index": True},
              "cost": {"type": "float", "index": False},
          }
      })
      resp = await client.post("/events", json={
          "source_id": "promo",
          "type": "metric",
          "data": {"model": "gpt-4", "cost": 0.03}
      })
      assert resp.status_code == 201


  @pytest.mark.asyncio
  async def test_strict_validation_rejects_missing_field(client):
      await client.post("/sources", json={"id": "strict1", "metadata": {}})
      await client.post("/schemas", json={
          "source_id": "strict1",
          "type": "metric",
          "schema": {"value": {"type": "float", "index": False}}
      })
      resp = await client.post("/events", json={
          "source_id": "strict1",
          "type": "metric",
          "data": {"other_field": 123}
      })
      assert resp.status_code == 400


  @pytest.mark.asyncio
  async def test_strict_validation_rejects_wrong_type(client):
      await client.post("/sources", json={"id": "strict2", "metadata": {}})
      await client.post("/schemas", json={
          "source_id": "strict2",
          "type": "metric",
          "schema": {"value": {"type": "integer", "index": False}}
      })
      resp = await client.post("/events", json={
          "source_id": "strict2",
          "type": "metric",
          "data": {"value": "not_a_number"}
      })
      assert resp.status_code == 400


  @pytest.mark.asyncio
  async def test_no_schema_skips_validation(client):
      await client.post("/sources", json={"id": "free", "metadata": {}})
      resp = await client.post("/events", json={
          "source_id": "free",
          "type": "freeform",
          "data": {"anything": "goes", "nested": {"ok": True}}
      })
      assert resp.status_code == 201


  @pytest.mark.asyncio
  async def test_schema_rotation(client):
      await client.post("/sources", json={"id": "rot", "metadata": {}})
      r1 = await client.post("/schemas", json={
          "source_id": "rot",
          "type": "metric",
          "schema": {"value": {"type": "float", "index": False}}
      })
      schema1_id = r1.json()["id"]

      r2 = await client.post("/schemas", json={
          "source_id": "rot",
          "type": "metric",
          "schema": {
              "value": {"type": "float", "index": False},
              "label": {"type": "string", "index": True},
          }
      })
      assert r2.status_code == 201

      old = await client.get(f"/schemas/{schema1_id}")
      assert old.json()["end_time"] is not None


  @pytest.mark.asyncio
  async def test_query_promoted_table(client):
      await client.post("/sources", json={"id": "q", "metadata": {}})
      await client.post("/schemas", json={
          "source_id": "q",
          "type": "metric",
          "schema": {
              "model": {"type": "string", "index": True},
              "cost": {"type": "float", "index": True},
          }
      })
      await client.post("/events", json={"source_id": "q", "type": "metric", "data": {"model": "gpt-4", "cost": 0.03}})
      await client.post("/events", json={"source_id": "q", "type": "metric", "data": {"model": "gpt-3.5", "cost": 0.001}})

      resp = await client.get("/promoted/q/metric", params={"cost_gt": "0.01"})
      assert resp.status_code == 200
      rows = resp.json()
      assert len(rows) == 1
      assert rows[0]["model"] == "gpt-4"


  @pytest.mark.asyncio
  async def test_query_promoted_unknown_field_rejected(client):
      await client.post("/sources", json={"id": "qf", "metadata": {}})
      await client.post("/schemas", json={
          "source_id": "qf",
          "type": "m",
          "schema": {"value": {"type": "float", "index": False}}
      })
      resp = await client.get("/promoted/qf/m", params={"nonexistent": "123"})
      assert resp.status_code == 400


  @pytest.mark.asyncio
  async def test_query_promoted_no_schema(client):
      resp = await client.get("/promoted/nosrc/notype")
      assert resp.status_code == 400


  @pytest.mark.asyncio
  async def test_webhook_with_source_id(client):
      resp = await client.post("/webhooks", json={
          "url": "http://example.com/hook",
          "source_id": "mysource",
          "event_types": ["metric"],
      })
      assert resp.status_code == 201
      data = resp.json()
      assert data["source_id"] == "mysource"


  @pytest.mark.asyncio
  async def test_close_engine_recreates_engine():
      await close_engine()
      await init_db()
      first_engine = get_engine()
      await close_engine()
      await init_db()
      second_engine = get_engine()
      assert first_engine is not second_engine
      await close_engine()
