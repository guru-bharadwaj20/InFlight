"""Retry with backoff and a circuit breaker for provider calls.

This project has actually been rate-limited in anger (the free Gemini tier is 20
requests/day/model), so provider failure is a first-class case, not a corner one.
Two mechanisms handle it:

  * **Retry with exponential backoff + jitter** for transient failures (429 /
    RESOURCE_EXHAUSTED, 503 / UNAVAILABLE). Jitter spreads a thundering herd of
    retries so N jobs that all got throttled at once do not all retry in lockstep.

  * **A circuit breaker.** Once the provider has failed `threshold` times in a
    row, the breaker opens and calls fail fast for a cooldown instead of piling
    more doomed requests onto a provider that is already down. After the cooldown
    it goes half-open and lets one trial through: success closes it, failure opens
    it again. This is what stops a provider outage turning into a queue of jobs
    all blocking on their own timeouts.

Retrying a *stream* is only safe before the first token — after that a retry
would duplicate text — so the streaming caller retries establishment only. The
non-streaming classifier call can be retried freely.
"""

from __future__ import annotations

import random
import time
from enum import Enum


class CircuitOpen(RuntimeError):
    """Raised when the breaker is open, so the call fails fast and visibly."""


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def is_transient(exc: BaseException) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in ("RESOURCE_EXHAUSTED", "429", "UNAVAILABLE", "503", "DEADLINE_EXCEEDED")
    )


def backoff(attempt: int, base: float, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter: random in [0, min(cap, base*2^n))."""
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, ceiling)


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown: float, name: str = "provider") -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.name = name
        self._failures = 0
        self._state = State.CLOSED
        self._opened_at = 0.0
        # Optional hook so telemetry can reflect state without importing this.
        self.on_state_change = None  # type: ignore[assignment]

    @property
    def state(self) -> State:
        return self._state

    def _set(self, state: State) -> None:
        if state != self._state:
            self._state = state
            if self.on_state_change is not None:
                self.on_state_change(state)

    def allow(self) -> bool:
        """Whether a call may proceed now, advancing open -> half-open on cooldown."""
        if self._state is State.OPEN:
            if time.monotonic() - self._opened_at >= self.cooldown:
                self._set(State.HALF_OPEN)
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._set(State.CLOSED)

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is State.HALF_OPEN or self._failures >= self.threshold:
            self._opened_at = time.monotonic()
            self._set(State.OPEN)
