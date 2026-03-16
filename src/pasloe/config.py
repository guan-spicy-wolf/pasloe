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
