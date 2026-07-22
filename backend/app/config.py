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

    # Swaps in a deterministic local generator instead of calling Gemini. Off by
    # default and never inferred from a missing key — a silent fallback to fake
    # answers would be far worse than a visible error. It exists so the
    # concurrency behaviour can be exercised repeatably and for free, which the
    # Stage 11 load test needs.
    use_fake_llm: bool = False
    fake_llm_chunk_delay_ms: int = 45

    # A dependent job waits for everything submitted before it. The cap stops one
    # wedged predecessor from stranding a job forever — past it, answering with a
    # slightly stale snapshot beats never answering at all.
    max_dependency_wait_seconds: int = 120
    dependency_poll_interval_ms: int = 150

    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
