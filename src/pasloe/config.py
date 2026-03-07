from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Database configuration
    # Set to "sqlite" or "postgres"
    db_type: str = "sqlite"

    # SQLite config
    sqlite_path: str = "./events.db"

    # Postgres config
    pg_user: str = "user"
    pg_password: str = "password"
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "pasloe"

    @property
    def database_url(self) -> str:
        if self.db_type == "postgres":
            return f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        return f"sqlite+aiosqlite:///{self.sqlite_path}"
    
    # API configuration
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str | None = None
    allow_insecure_http: bool = False
    
    # Webhooks
    webhook_max_retries: int = 3
    webhook_retry_backoff: float = 1.0

    # Environment name
    env: str = "dev"
    
    # S3 Configuration for artifact uploads
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_db_url() -> str:
    return get_settings().database_url

def is_sqlite() -> bool:
    return get_settings().db_type == "sqlite"
