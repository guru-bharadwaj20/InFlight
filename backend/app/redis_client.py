"""Redis scaffold: job status keys and the per-job streaming channel.

Nothing calls the publish/subscribe helpers yet — Stage 3 wires them into the
asyncio job tasks and the WebSocket fan-out. They live here now so the key
naming is decided in one place before there are several writers.

Key layout:
    job:{job_id}                    hash    status, conversation_id, timestamps
    job:{job_id}:buffer             string  text accumulated so far (survives a
                                            browser refresh mid-stream)
    conversation:{id}:active        set     job ids currently pending/streaming
    channel:job:{job_id}            pubsub  {type: chunk|status|done|error, ...}
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


def job_channel(job_id: str) -> str:
    return f"channel:job:{job_id}"


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


async def append_chunk(job_id: str, text: str) -> None:
    client = get_redis()
    async with client.pipeline(transaction=True) as pipe:
        pipe.append(job_buffer_key(job_id), text)
        pipe.expire(job_buffer_key(job_id), JOB_TTL_SECONDS)
        await pipe.execute()


async def get_buffer(job_id: str) -> str:
    return await get_redis().get(job_buffer_key(job_id)) or ""


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


async def publish(job_id: str, frame: dict[str, Any]) -> None:
    """Publish one frame for a job. Every frame carries its own job_id so the
    client can route it to the right bubble without relying on arrival order."""
    await get_redis().publish(job_channel(job_id), json.dumps({"job_id": job_id, **frame}))


@asynccontextmanager
async def subscribe(*job_ids: str) -> AsyncIterator[redis.client.PubSub]:
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    try:
        if job_ids:
            await pubsub.subscribe(*[job_channel(j) for j in job_ids])
        yield pubsub
    finally:
        await pubsub.aclose()


async def frames(pubsub: redis.client.PubSub) -> AsyncIterator[dict[str, Any]]:
    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        yield json.loads(message["data"])
