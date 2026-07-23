"""Prove the messages table is a projection of the event log.

    docker compose exec backend python -m scripts.event_replay

Runs a real job to completion, then rebuilds its state purely by folding the
append-only event stream (`events.project`) and asserts that reconstruction
matches the materialized row field for field. If it does, the row is provably a
cache of the log, which is what makes this event sourcing rather than logging.

Also demonstrates time travel: projecting the event *prefix* up to the streaming
event shows the job mid-flight (status streaming, no content yet), the state the
conversation was actually in at that instant.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator

from sqlalchemy import delete, select

from app import events, jobs, redis_client
from app.db import dispose_engine, init_engine, session_factory
from app.ids import new_id
from app.llm import Usage
from app.models import Conversation, DependencyMode, Message, Role, Status, User, utcnow


async def fake_stream(turns, usage: Usage, *, model=None, images=()) -> AsyncIterator[str]:
    usage.prompt_tokens = 7
    usage.completion_tokens = 0
    for i in range(5):
        usage.completion_tokens += 1
        yield f"word{i} "
        await asyncio.sleep(0.005)


async def _make_job(session, conversation_id: str) -> str:
    job_id = new_id()
    now = utcnow()
    prompt = Message(
        id=new_id(), conversation_id=conversation_id, role=Role.USER,
        content="hello", status=Status.COMPLETE,
        submitted_at=now, completed_at=now, context_cutoff=now,
    )
    session.add(prompt)
    await session.flush()
    answer = Message(
        id=job_id, conversation_id=conversation_id, role=Role.ASSISTANT,
        status=Status.PENDING, submitted_at=now, context_cutoff=now,
        prompt_message_id=prompt.id, dependency_mode=DependencyMode.AUTO,
        detected_dependency="independent", dependency_source="heuristic",
    )
    session.add(answer)
    await session.commit()
    await redis_client.set_job_state(
        job_id, status=Status.PENDING, conversation_id=conversation_id, seq=0
    )
    await redis_client.register_active_job(conversation_id, job_id)
    # The submit event the router would have appended.
    await events.append(conversation_id, events.SUBMITTED, job_id, verdict="independent")
    return job_id


async def main() -> int:
    logging.getLogger("app.jobs").setLevel(logging.CRITICAL)
    jobs.stream_completion = fake_stream  # type: ignore[assignment]

    init_engine()
    redis_client.init_redis()

    failures: list[str] = []
    async with session_factory()() as session:
        user = User(email=f"ev_{new_id()}@example.com", name="Ev", password_hash="x")
        session.add(user)
        await session.flush()
        conv = Conversation(user_id=user.id)
        session.add(conv)
        await session.commit()
        conversation_id = conv.id

        # Fresh stream for a clean assertion.
        await redis_client.get_redis().delete(f"events:{conversation_id}")

        job_id = await _make_job(session, conversation_id)
        await asyncio.gather(jobs.spawn(job_id, conversation_id), return_exceptions=True)

        # Authoritative row.
        row = (
            await session.execute(
                select(
                    Message.status, Message.content,
                    Message.prompt_tokens, Message.completion_tokens,
                ).where(Message.id == job_id)
            )
        ).one()
        materialized = {
            "status": row[0], "content": row[1],
            "prompt_tokens": row[2], "completion_tokens": row[3],
        }

        log = await events.read(conversation_id)
        projected = events.project(log).get(job_id, {})

        for field, expected in materialized.items():
            got = projected.get(field)
            if got != expected:
                failures.append(f"{field}: projected {got!r} != row {expected!r}")

        # Time travel: fold only up to the streaming event.
        prefix = []
        for e in log:
            prefix.append(e)
            if e["type"] == events.STREAMING:
                break
        mid = events.project(prefix).get(job_id, {})
        if mid.get("status") != "streaming":
            failures.append(f"time-travel: prefix status {mid.get('status')!r} != 'streaming'")

        event_types = [e["type"] for e in log]

        # Teardown.
        await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
        await redis_client.get_redis().delete(f"events:{conversation_id}")

    await redis_client.close_redis()
    await dispose_engine()

    print("event sourcing — projection vs materialized row")
    print(f"  event stream        {' -> '.join(event_types)}")
    print(f"  materialized        {materialized}")
    print(f"  projected from log  {projected}")
    if failures:
        print(f"  result              FAIL ({len(failures)})")
        for f in failures:
            print("   -", f)
        return 1
    print("  result              PASS — row is exactly the projection of the log; "
          "prefix replay shows the mid-stream state")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
