"""Fair, backpressure-aware admission for generation slots.

The per-conversation cap (an atomic Redis script) stops one conversation flooding
itself. It does nothing about the *process*: 200 conversations each sending one
prompt would launch 200 concurrent provider calls, and a burst from one user
would be served in whatever arbitrary order the event loop woke the tasks.

This adds a single admission point every job passes through before it calls the
model. It enforces two things a queue of independent `asyncio` tasks cannot:

  * a global concurrency bound (how many generations run at once, process-wide);
  * a token-bucket rate limit, so the provider is approached at a sustainable
    rate and an overload becomes *waiting* (backpressure) rather than a burst of
    429s.

And it grants slots **fairly**: waiters are keyed by conversation and served
round-robin, so a user who queues twenty prompts cannot starve a user who queued
one. A single dispatcher task owns all the accounting, so there are no locks and
no lost-wakeup races — every state change just sets one event the dispatcher
drains.

Submission is unaffected: the HTTP side still returns 202 immediately. The only
thing that waits is the job, which is exactly where waiting belongs.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from typing import AsyncIterator


class TokenBucket:
    """Classic token bucket: `rate` tokens accrue per second up to `capacity`.

    `try_take` is non-blocking; `time_until_token` tells the dispatcher how long
    to sleep before a token will exist, so it waits precisely instead of spinning.
    """

    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        self.rate = rate_per_sec
        self.capacity = max(1.0, capacity)
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_take(self) -> bool:
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def time_until_token(self) -> float:
        self._refill()
        if self.tokens >= 1 or self.rate <= 0:
            return 0.0
        return (1 - self.tokens) / self.rate


class FairScheduler:
    def __init__(self, max_concurrency: int, rate_per_min: float = 0.0) -> None:
        # max_concurrency <= 0 disables the gate entirely: slot() becomes a
        # no-op, so the scheduler can ship default-off and never change behaviour
        # until someone sets a real bound.
        self.max = max_concurrency
        self.bucket = (
            TokenBucket(rate_per_min / 60.0, capacity=max(1, max_concurrency))
            if rate_per_min and rate_per_min > 0
            else None
        )
        self.active = 0
        self._queues: OrderedDict[str, deque[asyncio.Future]] = OrderedDict()
        self._rr: deque[str] = deque()  # round-robin order of conversation keys
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # --- introspection (for metrics) ---
    def waiting(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def _pick_key(self) -> str | None:
        # Rotate through the round-robin order and return the first key that
        # actually has a waiter. Empty keys are skipped, not removed, so a
        # conversation keeps its place in the rotation across bursts.
        for _ in range(len(self._rr)):
            key = self._rr[0]
            self._rr.rotate(-1)
            q = self._queues.get(key)
            if q:
                return key
        return None

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.active < self.max and self.waiting() > 0:
                if self.bucket is not None and not self.bucket.try_take():
                    # No token yet: sleep exactly until one accrues, then retry.
                    delay = min(self.bucket.time_until_token(), 1.0)
                    asyncio.get_running_loop().call_later(delay, self._wake.set)
                    break
                key = self._pick_key()
                if key is None:
                    break
                fut = self._queues[key].popleft()
                if fut.done():
                    # Cancelled while queued — reclaim nothing, just skip it.
                    continue
                self.active += 1
                fut.set_result(None)

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _release(self) -> None:
        self.active -= 1
        self._wake.set()

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[float]:
        """Wait for a fair generation slot, then hold it for the duration of the
        block. Yields the seconds spent waiting (0 when the gate is disabled)."""
        if self.max <= 0:
            yield 0.0
            return

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._queues.setdefault(key, deque()).append(fut)
        if key not in self._rr:
            self._rr.append(key)
        self._ensure_running()
        self._wake.set()

        t0 = time.perf_counter()
        try:
            await fut
        except asyncio.CancelledError:
            # If the grant landed just before the cancel, we own a slot and must
            # give it back; otherwise the dispatcher already skipped the future.
            if fut.done() and not fut.cancelled():
                self._release()
            raise

        wait_s = time.perf_counter() - t0
        try:
            yield wait_s
        finally:
            self._release()


_scheduler: FairScheduler | None = None


def get_scheduler() -> FairScheduler:
    global _scheduler
    if _scheduler is None:
        from .config import get_settings

        s = get_settings()
        _scheduler = FairScheduler(
            max_concurrency=s.max_global_concurrency,
            rate_per_min=s.generation_rate_per_min,
        )
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
        _scheduler = None
