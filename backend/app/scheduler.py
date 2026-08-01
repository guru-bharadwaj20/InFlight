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


class SchedulerStopped(RuntimeError):
    """Raised into anything still queued for a slot when the scheduler stops, so
    a shutting-down worker's jobs fail fast instead of waiting on a dispatcher
    that will never run again."""


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
        # Membership mirror of _rr. `key in deque` is a linear scan, and slot()
        # tested it on every single acquisition, so admission cost grew with the
        # number of distinct conversations the process had ever seen.
        self._in_rr: set[str] = set()
        # Maintained incrementally rather than recomputed. waiting() was summing
        # over every queue, and both the dispatcher loop and every /metrics
        # scrape called it.
        self._waiting = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # --- introspection (for metrics) ---
    def waiting(self) -> int:
        return self._waiting

    def tracked_keys(self) -> int:
        """Conversations currently holding a place in the rotation. Should fall
        back to zero once everything drains — it is the leak canary."""
        return len(self._queues)

    def _drop_key(self, key: str) -> None:
        self._queues.pop(key, None)
        self._in_rr.discard(key)

    def _pick_key(self) -> str | None:
        # Rotate through the round-robin order and return the first key that
        # actually has a waiter, evicting any that no longer does.
        #
        # Empty keys used to be skipped rather than removed, "so a conversation
        # keeps its place in the rotation across bursts". The place is worth
        # nothing — round-robin fairness only orders keys that have someone
        # queued — and the cost was unbounded: one permanent entry in _queues and
        # _rr per conversation id ever admitted, for the life of the process,
        # doubled because the classifier queues under its own key. Every
        # acquisition then scanned that growing deque, and every dispatch
        # rotated through all of it.
        for _ in range(len(self._rr)):
            key = self._rr[0]
            if self._queues.get(key):
                self._rr.rotate(-1)  # keep it, but move it to the back
                return key
            self._rr.popleft()
            self._drop_key(key)
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
                self._waiting -= 1
                # The now-possibly-empty key is left for _pick_key to evict when
                # the rotation next brings it to the front. Removing it here
                # would mean deque.remove(), an O(n) scan on the hot path — the
                # very cost this change exists to remove. Lazy eviction is O(1)
                # amortised and every key reaches the front eventually.
                if fut.done():
                    # Cancelled while queued — reclaim nothing, just skip it.
                    continue
                self.active += 1
                fut.set_result(None)

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        """Shut the dispatcher down and fail anything still queued for a slot.

        Cancelling the dispatcher on its own left queued futures unresolved, so
        every job waiting for admission simply hung — it could only be freed by
        drain()'s timeout cancelling the whole task, which turns an orderly
        shutdown into a stall for as long as the drain window. Waking each waiter
        with SchedulerStopped lets those jobs settle through their normal error
        path instead.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for key, queue in list(self._queues.items()):
            while queue:
                fut = queue.popleft()
                self._waiting -= 1
                if not fut.done():
                    fut.set_exception(SchedulerStopped("scheduler is shutting down"))
            self._drop_key(key)
        self._rr.clear()

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
        # O(1) membership test. This was `key not in self._rr`, a linear scan of
        # a deque that only ever grew.
        if key not in self._in_rr:
            self._in_rr.add(key)
            self._rr.append(key)
        self._waiting += 1
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
            else:
                # Still queued and now dead. Wake the dispatcher so it pops and
                # discards it, rather than leaving it counted in _waiting (and
                # reported as backpressure in /metrics) until unrelated traffic
                # happens to wake the loop.
                self._wake.set()
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
