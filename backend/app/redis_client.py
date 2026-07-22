"""Redis: job status keys and the streaming fan-out.

The channel is per *conversation*, not per job, because a client holds one
WebSocket and every frame carries the job it belongs to. Subscribing per job
would mean re-subscribing each time a prompt is sent, and would race against
jobs that start between the client connecting and the subscription landing.

Key layout:
    job:{job_id}                    hash    status, conversation_id, seq
    job:{job_id}:buffer             string  text accumulated so far (survives a
                                            browser refresh mid-stream)
    conversation:{id}:active        set     job ids currently pending/streaming
    channel:conversation:{id}       pubsub  {job_id, type: chunk|status|done|error}
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as redis

from .config import get_settings

# In-flight job state is reconstructable from Postgres, so nothing here needs to
# outlive a generation by much. The TTL keeps abandoned jobs from accumulating.
JOB_TTL_SECONDS = 60 * 60


def job_key(job_id: str) -> str:
    return f"job:{job_id}"


def job_buffer_key(job_id: str) -> str:
    return f"job:{job_id}:buffer"


def conversation_active_key(conversation_id: str) -> str:
    return f"conversation:{conversation_id}:active"


def conversation_channel(conversation_id: str) -> str:
    return f"channel:conversation:{conversation_id}"


_client: redis.Redis | None = None


def init_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_redis() -> redis.Redis:
    if _client is None:
        return init_redis()
    return _client


async def ping() -> bool:
    return bool(await get_redis().ping())


# --- Job state ------------------------------------------------------------


async def set_job_state(job_id: str, **fields: Any) -> None:
    client = get_redis()
    payload = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    async with client.pipeline(transaction=True) as pipe:
        pipe.hset(job_key(job_id), mapping=payload)
        pipe.expire(job_key(job_id), JOB_TTL_SECONDS)
        await pipe.execute()


async def get_job_state(job_id: str) -> dict[str, str]:
    return await get_redis().hgetall(job_key(job_id))


async def append_chunk(job_id: str, text: str, seq: int) -> None:
    """Extend the replay buffer and record how many chunks it covers.

    Both in one transaction, so `job_snapshot` can never read a buffer and a
    sequence number that disagree about how much text has been emitted.
    """
    client = get_redis()
    async with client.pipeline(transaction=True) as pipe:
        pipe.append(job_buffer_key(job_id), text)
        pipe.hset(job_key(job_id), "seq", seq)
        pipe.expire(job_buffer_key(job_id), JOB_TTL_SECONDS)
        pipe.expire(job_key(job_id), JOB_TTL_SECONDS)
        await pipe.execute()


async def job_snapshot(job_id: str) -> tuple[dict[str, str], str]:
    """State and replay buffer for one job, read together.

    A client reconnecting mid-stream replays this and then ignores any chunk
    frame at or below the recorded `seq`, so the text it already has is never
    counted twice against the frames still arriving on the channel.
    """
    client = get_redis()
    async with client.pipeline(transaction=True) as pipe:
        pipe.hgetall(job_key(job_id))
        pipe.get(job_buffer_key(job_id))
        state, buffer = await pipe.execute()
    return state or {}, buffer or ""


async def clear_job(job_id: str, conversation_id: str | None = None) -> None:
    client = get_redis()
    async with client.pipeline(transaction=True) as pipe:
        pipe.delete(job_key(job_id), job_buffer_key(job_id))
        if conversation_id:
            pipe.srem(conversation_active_key(conversation_id), job_id)
        await pipe.execute()


# --- Active-job registry (per conversation) -------------------------------


async def register_active_job(conversation_id: str, job_id: str) -> None:
    await get_redis().sadd(conversation_active_key(conversation_id), job_id)


async def unregister_active_job(conversation_id: str, job_id: str) -> None:
    await get_redis().srem(conversation_active_key(conversation_id), job_id)


async def active_jobs(conversation_id: str) -> set[str]:
    return set(await get_redis().smembers(conversation_active_key(conversation_id)))


async def active_job_count(conversation_id: str) -> int:
    return int(await get_redis().scard(conversation_active_key(conversation_id)))


# --- Streaming fan-out ----------------------------------------------------


async def publish(conversation_id: str, job_id: str, frame: dict[str, Any]) -> None:
    """Publish one frame. Every frame carries its own job_id so the client can
    route it to the right bubble without relying on arrival order."""
    await get_redis().publish(
        conversation_channel(conversation_id), json.dumps({"job_id": job_id, **frame})
    )


@asynccontextmanager
async def subscribe(conversation_id: str) -> AsyncIterator[redis.client.PubSub]:
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(conversation_channel(conversation_id))
        yield pubsub
    finally:
        await pubsub.aclose()


async def frames(pubsub: redis.client.PubSub) -> AsyncIterator[dict[str, Any]]:
    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        yield json.loads(message["data"])
