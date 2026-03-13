from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

  from .config import get_db_url, is_sqlite
  from .models import Base

  _engine = None
  _SessionLocal = None


  def get_engine():
      global _engine
      if _engine is None:
          url = get_db_url()
          connect_args = {"check_same_thread": False} if is_sqlite() else {}
          _engine = create_async_engine(url, connect_args=connect_args, echo=False)
      return _engine


  def get_session_factory():
      global _SessionLocal
      if _SessionLocal is None:
          _SessionLocal = async_sessionmaker(get_engine(), expire_on_commit=False)
      return _SessionLocal


  async def init_db():
      """Create all tables and rebuild promoted table cache."""
      async with get_engine().begin() as conn:
          await conn.run_sync(Base.metadata.create_all)
      await _rebuild_promoted_tables()


  async def _rebuild_promoted_tables():
      """On startup, reconstruct Table objects for all schemas and ensure tables exist."""
      from .models import EventTypeSchemaRecord
      from .promoted import get_or_build_table, create_promoted_table
      from sqlalchemy import select

      factory = get_session_factory()
      async with factory() as session:
          result = await session.execute(select(EventTypeSchemaRecord))
          schemas = list(result.scalars().all())

      engine = get_engine()
      for schema_record in schemas:
          table = get_or_build_table(schema_record.table_name, schema_record.schema_)
          await create_promoted_table(engine, table)


  async def close_engine():
      """Dispose engine resources for graceful shutdown and test isolation."""
      global _engine, _SessionLocal
      if _engine is not None:
          await _engine.dispose()
      _engine = None
      _SessionLocal = None

      from .promoted import clear_table_cache
      clear_table_cache()


  async def get_session() -> AsyncSession:
      factory = get_session_factory()
      async with factory() as session:
          try:
              yield session
              await session.commit()
          except Exception:
              await session.rollback()
              raise
