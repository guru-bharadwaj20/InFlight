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


# Shared with the reserve script's liveness probe, so the two can't drift apart.
_JOB_KEY_PREFIX = "job:"


def job_key(job_id: str) -> str:
    return f"{_JOB_KEY_PREFIX}{job_id}"


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


async def clear_buffer(job_id: str) -> None:
    """Drop replayed text before a re-run, so the new answer starts empty."""
    await get_redis().delete(job_buffer_key(job_id))


def job_attachments_key(job_id: str) -> str:
    return f"job:{job_id}:attachments"


async def set_attachments(job_id: str, attachments: list[dict]) -> None:
    """Stash a prompt's images for the job to read once, keyed by job id.

    Passed through Redis rather than a database column because they matter only
    to the single generation that was submitted with them — not persisted, not
    redisplayed, not part of any later job's context.
    """
    if not attachments:
        return
    client = get_redis()
    await client.set(
        job_attachments_key(job_id), json.dumps(attachments), ex=JOB_TTL_SECONDS
    )


async def get_attachments(job_id: str) -> list[dict]:
    raw = await get_redis().get(job_attachments_key(job_id))
    return json.loads(raw) if raw else []


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


async def clear_job(
    job_id: str, conversation_id: str | None = None, *, drop_attachments: bool = True
) -> None:
    """Drop a job's live state, and by default its attachments too.

    `drop_attachments=False` is for a job reaching a *terminal* state: the answer
    is committed, but the row can still be regenerated, and a regenerate re-runs
    the same prompt — which for a vision prompt means it still needs the images
    that prompt was sent with. Deleting them on completion made regenerate answer
    an image question with no image, silently and with no error. They expire on
    their own via JOB_TTL_SECONDS, so this defers the cleanup rather than
    skipping it.
    """
    client = get_redis()
    keys = [job_key(job_id), job_buffer_key(job_id)]
    if drop_attachments:
        keys.append(job_attachments_key(job_id))
    async with client.pipeline(transaction=True) as pipe:
        pipe.delete(*keys)
        if conversation_id:
            pipe.srem(conversation_active_key(conversation_id), job_id)
        await pipe.execute()


# --- Active-job registry (per conversation) -------------------------------


# Checking the count and then adding cannot be done as two round trips: N
# simultaneous sends all read the same count before any of them registers, and
# every one of them is admitted. Redis runs a script atomically, so the test and
# the insert cannot be interleaved.
#
# The membership count is taken over *live* jobs, not raw set members. Every
# other key here expires, but this set only ever shrank by explicit SREM, so any
# path that reserved a slot and then died before releasing it — a SIGKILL between
# the reserve and the spawn, a worker lost mid-generation, a failed clear_job —
# burned one of the conversation's slots for good. Eight such events and that
# conversation returned 429 forever, with nothing in the UI able to clear it.
#
# A live job always has a `job:<id>` hash, refreshed with a TTL on every chunk,
# so its absence is exactly the signal that a member is dead. Pruning them here
# makes the cap self-healing: a leaked slot comes back on its own once the job
# hash ages out, instead of never. The set is small (bounded by the limit in
# normal operation), so SMEMBERS is cheap.
#
# Key names are built inside the script, which is fine on a single instance but
# would need hash tags under Redis Cluster.
_RESERVE_SCRIPT = """
local members = redis.call('SMEMBERS', KEYS[1])
local live = 0
for i = 1, #members do
  if redis.call('EXISTS', ARGV[4] .. members[i]) == 1 then
    live = live + 1
  else
    redis.call('SREM', KEYS[1], members[i])
  end
end
if live >= tonumber(ARGV[2]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


async def reserve_active_job(conversation_id: str, job_id: str, limit: int) -> bool:
    """Claim a concurrency slot, or return False if the conversation is full."""
    admitted = await get_redis().eval(
        _RESERVE_SCRIPT,
        1,
        conversation_active_key(conversation_id),
        job_id,
        limit,
        JOB_TTL_SECONDS,
        _JOB_KEY_PREFIX,
    )
    return bool(admitted)


async def register_active_job(conversation_id: str, job_id: str) -> None:
    key = conversation_active_key(conversation_id)
    async with get_redis().pipeline(transaction=True) as pipe:
        pipe.sadd(key, job_id)
        # Bound the set's own lifetime too, so a conversation that goes quiet
        # cannot leave the key behind indefinitely.
        pipe.expire(key, JOB_TTL_SECONDS)
        await pipe.execute()


async def unregister_active_job(conversation_id: str, job_id: str) -> None:
    await get_redis().srem(conversation_active_key(conversation_id), job_id)


async def active_jobs(conversation_id: str) -> set[str]:
    return set(await get_redis().smembers(conversation_active_key(conversation_id)))


async def active_job_count(conversation_id: str) -> int:
    return int(await get_redis().scard(conversation_active_key(conversation_id)))


# --- Idempotency keys -----------------------------------------------------
#
# A prompt submission is not naturally idempotent: retry it and you get a second
# job. An Idempotency-Key lets the client mark two sends as the same intent; the
# first claims the key and creates the job, and any duplicate returns the first
# result instead of creating another.

IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24

# The *claim* is short-lived, unlike the recorded result. A claim only has to
# outlive the handful of milliseconds between reserving the key and committing
# the rows. Giving it the full 24-hour result TTL meant that a worker dying in
# that window left the key stuck on "pending" for a day: every honest retry of
# that same request then waited a second for a result that was never coming and
# got a 409, with no way to clear it. Expiring the claim quickly turns that
# permanent wedge into a brief one that resolves itself.
IDEMPOTENCY_CLAIM_TTL_SECONDS = 60


async def claim_idempotency(key: str) -> bool:
    """Atomically claim an idempotency key (SET NX). False means it already exists."""
    got = await get_redis().set(
        f"idem:{key}", "pending", nx=True, ex=IDEMPOTENCY_CLAIM_TTL_SECONDS
    )
    return bool(got)


async def store_idempotency(key: str, value: str) -> None:
    """Record the result under an already-claimed key, keeping the TTL."""
    await get_redis().set(f"idem:{key}", value, ex=IDEMPOTENCY_TTL_SECONDS)


async def get_idempotency(key: str) -> str | None:
    return await get_redis().get(f"idem:{key}")


async def release_idempotency(key: str) -> None:
    """Drop a claim whose creation failed, so an honest retry is not blocked."""
    await get_redis().delete(f"idem:{key}")


# --- WebSocket tickets ----------------------------------------------------
#
# A browser cannot set an Authorization header on a WebSocket, so the credential
# has to travel in the URL. Sending the *session token* there put a week-long
# credential into places URLs habitually end up: proxy and access logs, browser
# history, Referer headers, error trackers. A ticket is the standard way out —
# an opaque, single-use, seconds-long stand-in that is worthless by the time any
# log containing it is read.

WS_TICKET_TTL_SECONDS = 30


def _ws_ticket_key(ticket: str) -> str:
    return f"ws-ticket:{ticket}"


async def create_ws_ticket(ticket: str, user_id: str) -> None:
    await get_redis().set(_ws_ticket_key(ticket), user_id, ex=WS_TICKET_TTL_SECONDS)


async def consume_ws_ticket(ticket: str) -> str | None:
    """Redeem a ticket, returning its user id. Single use: a replay finds nothing.

    GETDEL so the read and the invalidation are one atomic step — with a separate
    GET then DELETE, two connections racing the same ticket could both be
    admitted before either deleted it.
    """
    if not ticket:
        return None
    return await get_redis().getdel(_ws_ticket_key(ticket))


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


# --- Cross-worker control plane -------------------------------------------
#
# A job's asyncio task lives in exactly one worker's event loop, so a cancel
# request that lands on a *different* worker cannot reach it directly. This
# broadcast channel bridges them: every worker subscribes, and the one that owns
# the task acts on the message. Ownership stays implicit — whoever holds the task
# is the only worker whose local registry contains it — so no distributed lock or
# lease is needed, only the fan-out.

CONTROL_CHANNEL = "channel:control"


async def publish_control(payload: dict[str, Any]) -> None:
    await get_redis().publish(CONTROL_CHANNEL, json.dumps(payload))


@asynccontextmanager
async def subscribe_control() -> AsyncIterator[redis.client.PubSub]:
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(CONTROL_CHANNEL)
        yield pubsub
    finally:
        await pubsub.aclose()
