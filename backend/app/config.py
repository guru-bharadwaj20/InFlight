from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://chat:chat@localhost:5432/concurrent_chat"
    redis_url: str = "redis://localhost:6379/0"

    gemini_api_key: str = ""
    generation_model: str = "gemini-2.5-flash"
    # Deliberately a cheaper/faster model than the generation one: the Stage 7
    # dependency check runs in the submit path, so its latency is a tax on every
    # ambiguous prompt.
    classifier_model: str = "gemini-2.5-flash-lite"

    max_concurrent_jobs_per_conversation: int = 8

    # Process-wide admission control (app/scheduler.py). The per-conversation cap
    # above stops one chat flooding itself; these bound the whole worker and keep
    # the provider approached at a sustainable rate. A generous default that does
    # not change behaviour under normal load — the fair queuing only engages once
    # more than this many generations are genuinely in flight at once. Set to 0
    # to disable the gate entirely.
    max_global_concurrency: int = 64
    # Requests/minute permitted to the model, smoothed by a token bucket. 0 means
    # unlimited; set it to your provider's real limit to turn an overload into
    # backpressure (waiting) instead of a burst of 429s.
    generation_rate_per_min: float = 0.0

    # Signs the auth tokens. The default is fine for local development; set
    # JWT_SECRET in .env for anything that leaves this machine, or every token
    # is forgeable.
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_hours: int = 24 * 7

    # Swaps in a deterministic local generator instead of calling Gemini. Off by
    # default and never inferred from a missing key — a silent fallback to fake
    # answers would be far worse than a visible error. It exists so the
    # concurrency behaviour can be exercised repeatably and for free, which the
    # Stage 11 load test needs.
    use_fake_llm: bool = False
    fake_llm_chunk_delay_ms: int = 45

    # Provider resilience (app/resilience.py). Transient failures (429/503) are
    # retried with jittered exponential backoff; after enough consecutive
    # failures the circuit opens and calls fail fast for a cooldown rather than
    # piling more doomed requests onto a provider that is already down.
    provider_max_retries: int = 3
    provider_retry_base_seconds: float = 1.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_seconds: float = 30.0

    # A dependent job waits for everything submitted before it. The cap stops one
    # wedged predecessor from stranding a job forever — past it, answering with a
    # slightly stale snapshot beats never answering at all.
    max_dependency_wait_seconds: int = 120
    dependency_poll_interval_ms: int = 150

    # Speculative execution (optimistic concurrency). When on, an auto-detected
    # *dependent* prompt does not block: it generates immediately against the
    # current snapshot, and only if the retrospective check later confirms it
    # missed context does it abort and re-run once against the fuller snapshot.
    # The latency win is every speculation that turns out not to conflict.
    # Explicit chains still wait — the user asked for that ordering by hand.
    # Off by default: auto-rerunning costs a second model call.
    speculative_execution: bool = False

    cors_origins: str = "http://localhost:3000"

    # Structured logging (app/logging_config.py). JSON by default so lines carry
    # request/job correlation ids for a log pipeline; set LOG_JSON=false for
    # readable lines in a terminal.
    log_level: str = "INFO"
    log_json: bool = True

    # On shutdown, stop accepting traffic (readiness flips to 503) and wait up to
    # this long for in-flight generations to finish before cancelling stragglers.
    drain_timeout_seconds: float = 20.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
