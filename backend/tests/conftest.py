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

from app import db, redis_client


@pytest.fixture(autouse=True)
async def reset_pooled_clients():
    yield
    await redis_client.close_redis()
    await db.dispose_engine()
