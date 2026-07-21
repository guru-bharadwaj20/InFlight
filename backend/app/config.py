from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://chat:chat@localhost:5432/concurrent_chat"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    generation_model: str = "claude-opus-4-8"
    classifier_model: str = "claude-haiku-4-5"

    max_concurrent_jobs_per_conversation: int = 8

    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
