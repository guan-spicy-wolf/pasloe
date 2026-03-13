from contextlib import asynccontextmanager
  from fastapi import FastAPI

  from .database import init_db, close_engine
  from .api import router
  from fastapi.responses import HTMLResponse
  import os


  @asynccontextmanager
  async def lifespan(app: FastAPI):
      await init_db()
      from .config import get_settings
      if not get_settings().allow_insecure_http:
          print("\n" + "!" * 60)
          print("  SECURITY WARNING: Pasloe is running in secure mode.")
          print("  Ensure you are using HTTPS or set ALLOW_INSECURE_HTTP=True.")
          print("!" * 60 + "\n")
      try:
          yield
      finally:
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
      return {"status": "ok"}


  @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
  async def serve_ui():
      ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
      with open(ui_path, "r", encoding="utf-8") as f:
          return f.read()
