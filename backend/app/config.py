from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://chat:chat@localhost:5432/concurrent_chat"
    redis_url: str = "redis://localhost:6379/0"

    gemini_api_key: str = ""
    generation_model: str = "gemini-3.6-flash"
    # Deliberately a cheaper/faster model than the generation one: the Stage 7
    # dependency check runs in the submit path, so its latency is a tax on every
    # ambiguous prompt.
    classifier_model: str = "gemini-3.5-flash-lite"

    max_concurrent_jobs_per_conversation: int = 8

    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
