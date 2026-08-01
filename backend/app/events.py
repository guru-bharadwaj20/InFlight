"""An append-only event log for every job's lifecycle, on Redis Streams.

The `messages` table holds *current* state — one row per job, mutated in place as
it runs. That is the read model. This is the write-ahead history behind it: every
transition (submitted, resolved, streaming, completed, failed, cancelled, stale,
regenerated) is appended as an immutable event to a per-conversation stream, in
order, never edited.

Redis Streams are the store because they are exactly an append-only log — `XADD`
only ever appends, `XRANGE` replays in order — so nothing here can rewrite the
past even by accident, and no schema migration is needed to add it.

Two things fall out of having the log:

  * **Projection.** `project()` folds a conversation's events back into per-message
    state. That it reproduces the materialized row is the property that makes this
    event sourcing and not just logging: the table is a cache of the log, not a
    second source of truth. `scripts.event_replay` asserts they agree.
  * **Time travel.** Truncate the event list at any point and project the prefix
    to see exactly what the conversation looked like at that instant.

Emission is best-effort: a failure to append an audit event must never fail the
generation it describes, so callers wrap it and move on.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import redis_client, telemetry

logger = logging.getLogger(__name__)

# Cap each conversation's stream so it cannot grow without bound; the tail is
# what matters for audit and replay of a live conversation.
STREAM_MAXLEN = 10_000


# Event types — the full alphabet of a job's life.
SUBMITTED = "submitted"
RESOLVED = "resolved"
STREAMING = "streaming"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
STALE_FLAGGED = "stale_flagged"
REGENERATED = "regenerated"


def _stream_key(conversation_id: str) -> str:
    return f"events:{conversation_id}"


async def append(
    conversation_id: str, event_type: str, message_id: str | None = None, **data: Any
) -> None:
    """Append one event. Never raises — an audit failure must not fail the job."""
    try:
        fields = {
            "type": event_type,
            "message_id": message_id or "",
            "data": json.dumps(data, default=str),
        }
        await redis_client.get_redis().xadd(
            _stream_key(conversation_id), fields, maxlen=STREAM_MAXLEN, approximate=True
        )
    except Exception:
        # This is the only signal an append failure leaves: nothing re-raises,
        # so without a counter the event log's reliability is invisible except
        # by grepping logs.
        telemetry.EVENT_APPEND_FAILURES.labels(event_type=event_type).inc()
        logger.exception("failed to append %s event for %s", event_type, conversation_id)


# How many entries to pull per XRANGE round trip while paging. Bounded so one
# conversation's history never arrives as a single multi-megabyte reply.
_PAGE_SIZE = 1000


async def read(conversation_id: str, count: int = STREAM_MAXLEN) -> list[dict]:
    """The conversation's events in append order, oldest first.

    Pages rather than taking a single capped XRANGE. The old default read the
    first 1000 entries of a stream that holds up to STREAM_MAXLEN (10_000), so
    any conversation past a thousand events had its log silently cut to the
    *oldest* thousand — and `project()` then folded that prefix into a read model
    that could not match the current rows. An endpoint whose whole point is
    showing the table is a projection of the log must not project a fragment of
    it without saying so.
    """
    client = redis_client.get_redis()
    events: list[dict] = []
    cursor = "-"

    while len(events) < count:
        batch = await client.xrange(
            _stream_key(conversation_id),
            min=cursor,
            max="+",
            count=min(_PAGE_SIZE, count - len(events)),
        )
        if not batch:
            break
        for entry_id, fields in batch:
            events.append(
                {
                    "id": entry_id,
                    "type": fields.get("type"),
                    "message_id": fields.get("message_id") or None,
                    "data": json.loads(fields.get("data") or "{}"),
                }
            )
        # Exclusive ranges ("(<id>") need Redis 6.2+; the pinned image is 7.
        cursor = f"({batch[-1][0]}"

    return events


async def length(conversation_id: str) -> int:
    """How many events the stream actually holds, so a caller can tell whether
    what it read is the whole history or a truncated view of it."""
    try:
        return int(await redis_client.get_redis().xlen(_stream_key(conversation_id)))
    except Exception:
        logger.exception("failed to read stream length for %s", conversation_id)
        return 0


def project(events: list[dict]) -> dict[str, dict]:
    """Fold events into per-message current state — the read model, rebuilt.

    Deliberately independent of the SQLAlchemy write path: if this reproduces the
    materialized row, the row is provably a projection of the log and nothing
    more.
    """
    state: dict[str, dict] = {}
    for event in events:
        mid = event["message_id"]
        if not mid:
            continue
        data = event["data"]
        row = state.setdefault(
            mid, {"status": None, "content": None, "prompt_tokens": None, "completion_tokens": None}
        )
        etype = event["type"]
        if etype == SUBMITTED:
            row["status"] = "pending"
        elif etype == RESOLVED:
            row["detected_dependency"] = data.get("verdict")
        elif etype == STREAMING:
            row["status"] = "streaming"
        elif etype == COMPLETED:
            row["status"] = "complete"
            row["content"] = data.get("content")
            row["prompt_tokens"] = data.get("prompt_tokens")
            row["completion_tokens"] = data.get("completion_tokens")
        elif etype == FAILED:
            row["status"] = "error"
        elif etype == CANCELLED:
            row["status"] = "cancelled"
            row["content"] = data.get("content")
        elif etype == REGENERATED:
            # A re-run resets the row to pending; earlier terminal state is
            # superseded, exactly as restart_job does to the table.
            row["status"] = "pending"
            row["content"] = None
            row["prompt_tokens"] = None
            row["completion_tokens"] = None
        elif etype == STALE_FLAGGED:
            row["stale_context_reason"] = data.get("reason")
    return state
