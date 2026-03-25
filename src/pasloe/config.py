from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    db_type: str = "postgres"
    sqlite_path: str = "./events.db"
    pg_user: str = "yoitsu"
    pg_password: str = "yoitsu"
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_db: str = "pasloe"

    # API
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str | None = None
    allow_insecure_http: bool = False

    # Pipelines
    pipeline_poll_interval_seconds: float = 0.5
    pipeline_batch_size: int = 64
    pipeline_lease_seconds: int = 30
    pipeline_retry_base_seconds: float = 1.0
    pipeline_retry_max_seconds: float = 60.0
    health_max_oldest_uncommitted_age_seconds: float = 60.0

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
