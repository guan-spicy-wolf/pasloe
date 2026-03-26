from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import store
from .api import router
from .config import get_settings
from .database import close_engine, get_session_factory, init_db
from .pipeline import PipelineConfig, PipelineRuntime


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    from .projections import ProjectionRegistry

    app.state.projection_registry = ProjectionRegistry([])
    app.state.pipeline_runtime = PipelineRuntime(
        session_factory=get_session_factory(),
        projection_registry=app.state.projection_registry,
        config=PipelineConfig(
            poll_interval_seconds=settings.pipeline_poll_interval_seconds,
            batch_size=settings.pipeline_batch_size,
            lease_seconds=settings.pipeline_lease_seconds,
            retry_base_seconds=settings.pipeline_retry_base_seconds,
            retry_max_seconds=settings.pipeline_retry_max_seconds,
        ),
    )

    if not settings.allow_insecure_http:
        print("\n" + "!" * 60)
        print("  SECURITY WARNING: Pasloe is running in secure mode.")
        print("  Ensure you are using HTTPS or set ALLOW_INSECURE_HTTP=True.")
        print("!" * 60 + "\n")

    pipeline_started = False
    try:
        await app.state.pipeline_runtime.start()
        pipeline_started = True
        yield
    finally:
        if pipeline_started:
            await app.state.pipeline_runtime.stop()
        await close_engine()


app = FastAPI(
    title="Pasloe EventStore",
    description="Semantically-agnostic append-only event store with schema-driven data promotion.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
async def health():
    """Cheap, deterministic health check for load balancers and container orchestrators.
    
    Returns 200 OK immediately without database queries to avoid health flaps
    during normal operation (event polling, startup, etc.).
    """
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness probe that checks database connectivity and pipeline health.
    
    This is more expensive than /health and may return non-200 during startup
    or if the database is unavailable. Use for readiness checks, not liveness.
    """
    settings = get_settings()
    async with get_session_factory()() as db:
        stats = await store.get_stats(db)

    oldest_uncommitted = float(stats.get("oldest_uncommitted_age_s", 0.0))
    status = (
        "ok"
        if oldest_uncommitted <= settings.health_max_oldest_uncommitted_age_seconds
        else "degraded"
    )
    return {
        "status": status,
        "oldest_uncommitted_age_s": oldest_uncommitted,
        "ingress_pending": int(stats.get("ingress_pending", 0)),
        "outbox_pending": int(stats.get("outbox_pending", 0)),
        "outbox_pending_by_pipeline": stats.get("outbox_pending_by_pipeline", {}),
    }


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def serve_ui():
    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
    with open(ui_path, "r", encoding="utf-8") as f:
        return f.read()
