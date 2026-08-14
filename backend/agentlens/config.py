from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Local/offline mode uses an in-process shared SQLite database. Docker overrides
    # this with PostgreSQL, which is the durable deployment store.
    database_url: str = "sqlite+pysqlite:///:memory:"
    redis_url: str = "redis://localhost:6379/0"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    judge_model: str = "gpt-4.1-mini"
    bootstrap_samples: int = 1_500


@lru_cache
def get_settings() -> Settings:
    return Settings()
