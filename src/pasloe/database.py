from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_db_url, is_sqlite
from .models import Base

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_db_url()
        if is_sqlite():
            connect_args = {"check_same_thread": False, "timeout": 30}
            _engine = create_async_engine(
                url,
                connect_args=connect_args,
                echo=False,
            )
        else:
            _engine = create_async_engine(
                url,
                pool_pre_ping=True,
                pool_size=20,
                max_overflow=20,
                echo=False,
            )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _SessionLocal


async def init_db():
    """Create all tables."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_engine():
    """Dispose engine resources for graceful shutdown and test isolation."""
    global _engine, _SessionLocal
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _SessionLocal = None


async def get_session() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
