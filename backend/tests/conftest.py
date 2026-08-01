"""Per-test isolation for the connection pools the app keeps in module globals.

app.db and app.redis_client each cache their client in a process-global, and both
pool connections that belong to whichever event loop opened them. pytest-asyncio
runs every test on a fresh loop, so without this the second test to touch the
database checks out a connection created under the first test's loop and asyncpg
fails the request with "got Future attached to a different loop".

Disposing after each test keeps every pool inside the loop that created it. The
sync WebSocket test needs nothing extra: it goes through TestClient, which runs
the app's lifespan, and the shutdown half disposes both clients on its own portal
loop -- by the time this fixture tears down there is nothing left to close.
"""

import pytest
import redis.asyncio as aioredis

from app import db, redis_client
from app.config import get_settings


async def _clear_rate_limits() -> None:
    """Drop rate-limit counters so tests don't throttle each other.

    Every test signs up through the same ASGI transport, which presents one
    client address, so they all share a bucket. Without this the suite exhausts
    the signup limit partway through and later tests fail on a 429 that has
    nothing to do with what they are asserting.

    The limits stay *enabled* — the dependency and its Redis round trip still run
    on every request, so the suite keeps exercising that path. Only the
    accumulated counts are reset, which is what isolates one test from the next.

    Uses a throwaway connection rather than the app's pooled client on purpose.
    Touching the module-global client during setup leaves it bound to this
    fixture's event loop, which breaks the sync WebSocket test: that one drives
    the app through TestClient on its own portal loop and expects to find the
    globals untouched.
    """
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        keys = await client.keys("ratelimit:*")
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


@pytest.fixture(autouse=True)
async def reset_pooled_clients():
    await _clear_rate_limits()
    yield
    await _clear_rate_limits()
    await redis_client.close_redis()
    await db.dispose_engine()
