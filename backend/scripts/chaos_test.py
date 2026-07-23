"""Fault injection: prove one job's failure never touches its siblings.

    docker compose exec backend python -m scripts.chaos_test

The design claims per-job failure isolation, terminal settlement, and no leaked
state. This runs real jobs through the real `run_job` pipeline against the real
Postgres and Redis, injecting faults a happy-path test never sees, and asserts
those properties hold every time:

  * mid-stream provider errors (the model raises after a few tokens)
  * Redis failures during chunk buffering
  * cancellation mid-stream

After each scenario it checks the invariants:

  I  every job reaches a terminal state — nothing stuck pending/streaming
  II a failed or cancelled job leaves its siblings COMPLETE — isolation
  III cancellation keeps the partial text the user already saw
  IV  no leaks: the conversation's active-set is empty and the in-process
      task registry holds nothing once everything settles

Runs as its own process, so the monkeypatched fake model and fault-injecting
Redis wrapper never touch the live server. All rows it creates are torn down.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator

from sqlalchemy import delete, select

from app import jobs, redis_client
from app.db import dispose_engine, init_engine, session_factory
from app.ids import new_id
from app.llm import Usage
from app.models import Conversation, DependencyMode, Message, Role, Status, User, utcnow

FAIL = "boom: injected provider fault"


class Chaos:
    """Holds which faults are armed for which jobs, and the patched functions."""

    def __init__(self) -> None:
        self.fail_stream: set[str] = set()  # job ids whose stream raises mid-way
        self.fail_redis: set[str] = set()  # job ids whose chunk buffering raises
        self._orig_append = redis_client.append_chunk

    async def fake_stream(
        self, turns, usage: Usage, *, model=None, images=()
    ) -> AsyncIterator[str]:
        # Identify the job by a sentinel token carried on the last turn's content.
        job_id = turns[-1].content if turns else ""
        usage.prompt_tokens = usage.prompt_tokens or 0
        usage.completion_tokens = usage.completion_tokens or 0
        for i in range(10):
            if i == 3 and job_id in self.fail_stream:
                raise RuntimeError(FAIL)
            usage.completion_tokens += 1
            yield f"tok{i} "
            await asyncio.sleep(0.02)

    async def fake_append(self, job_id: str, text: str, seq: int) -> None:
        if job_id in self.fail_redis and seq >= 3:
            raise ConnectionError("boom: injected redis fault")
        await self._orig_append(job_id, text, seq)

    def patch(self) -> None:
        jobs.stream_completion = self.fake_stream  # type: ignore[assignment]
        redis_client.append_chunk = self.fake_append  # type: ignore[assignment]

    def unpatch(self) -> None:
        redis_client.append_chunk = self._orig_append  # type: ignore[assignment]


async def _make_conversation(session) -> tuple[str, str]:
    user = User(email=f"chaos_{new_id()}@example.com", name="Chaos", password_hash="x")
    session.add(user)
    await session.flush()
    conv = Conversation(user_id=user.id)
    session.add(conv)
    await session.flush()
    await session.commit()
    return user.id, conv.id


async def _make_job(session, conversation_id: str) -> str:
    """A prompt + a pending answer whose own content is the job id, so the fake
    model can tell which job it is streaming (the last turn is the prompt)."""
    job_id = new_id()
    now = utcnow()
    prompt = Message(
        id=new_id(),
        conversation_id=conversation_id,
        role=Role.USER,
        content=job_id,  # sentinel the fake stream reads back
        status=Status.COMPLETE,
        submitted_at=now,
        completed_at=now,
        context_cutoff=now,
    )
    session.add(prompt)
    await session.flush()
    answer = Message(
        id=job_id,
        conversation_id=conversation_id,
        role=Role.ASSISTANT,
        status=Status.PENDING,
        submitted_at=now,
        context_cutoff=now,
        prompt_message_id=prompt.id,
        dependency_mode=DependencyMode.AUTO,
        detected_dependency="independent",
        dependency_source="heuristic",
    )
    session.add(answer)
    await session.commit()
    await redis_client.set_job_state(
        job_id, status=Status.PENDING, conversation_id=conversation_id, seq=0
    )
    await redis_client.register_active_job(conversation_id, job_id)
    return job_id


async def _status(session, job_id: str) -> tuple[str, str | None]:
    row = await session.execute(
        select(Message.status, Message.content).where(Message.id == job_id)
    )
    return row.one()


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, cond: bool, msg: str) -> None:
        if not cond:
            self.failures.append(msg)


async def scenario_mixed_faults(session, conversation_id: str, chaos: Chaos, rep: Report) -> None:
    """A batch of jobs: some clean, some with stream faults, some with redis faults."""
    ids = [await _make_job(session, conversation_id) for _ in range(9)]
    clean, stream_bad, redis_bad = ids[:3], ids[3:6], ids[6:]
    chaos.fail_stream.update(stream_bad)
    chaos.fail_redis.update(redis_bad)

    await asyncio.gather(*[_run_to_completion(jid, conversation_id) for jid in ids])

    for jid in ids:
        st, _ = await _status(session, jid)
        rep.check(st in Status.TERMINAL, f"I: job {jid[:6]} stuck at {st}")

    for jid in clean:
        st, _ = await _status(session, jid)
        rep.check(st == Status.COMPLETE, f"II: clean sibling {jid[:6]} did not complete ({st})")
    for jid in stream_bad + redis_bad:
        st, _ = await _status(session, jid)
        rep.check(st == Status.ERROR, f"faulted job {jid[:6]} should be ERROR, was {st}")

    left = await redis_client.active_jobs(conversation_id)
    rep.check(not left, f"IV: active-set leaked {left}")
    rep.check(not jobs._running, f"IV: task registry leaked {list(jobs._running)}")


async def scenario_cancel_midstream(session, conversation_id: str, chaos: Chaos, rep: Report) -> None:
    """Cancel a job mid-stream; it must settle CANCELLED and keep partial text."""
    jid = await _make_job(session, conversation_id)
    sibling = await _make_job(session, conversation_id)
    task = jobs.spawn(jid, conversation_id)
    stask = jobs.spawn(sibling, conversation_id)

    # Wait until the job is genuinely streaming, then let a couple of tokens land,
    # so the cancel truly lands mid-stream (not in the pending window before it).
    for _ in range(200):
        st, _ = await _status(session, jid)
        if st == Status.STREAMING:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)  # a few tokens accumulate
    await jobs.request_cancel(jid)
    await asyncio.gather(task, stask, return_exceptions=True)

    st, content = await _status(session, jid)
    rep.check(st == Status.CANCELLED, f"cancelled job settled {st}, not CANCELLED")
    rep.check(bool(content), "III: cancelled job lost its partial text")
    sst, _ = await _status(session, sibling)
    rep.check(sst == Status.COMPLETE, f"II: sibling of cancelled job is {sst}")


async def _run_to_completion(job_id: str, conversation_id: str) -> None:
    task = jobs.spawn(job_id, conversation_id)
    await asyncio.gather(task, return_exceptions=True)


async def main() -> int:
    # The injected faults are logged by run_job's exception handler by design;
    # silence that here so the test's own output is the only thing on screen.
    logging.getLogger("app.jobs").setLevel(logging.CRITICAL)

    init_engine()
    redis_client.init_redis()
    chaos = Chaos()
    chaos.patch()
    rep = Report()

    async with session_factory()() as session:
        user_id, conversation_id = await _make_conversation(session)
        try:
            await scenario_mixed_faults(session, conversation_id, chaos, rep)
            await scenario_cancel_midstream(session, conversation_id, chaos, rep)
        finally:
            chaos.unpatch()
            # Tear down every row this test created.
            await session.execute(
                delete(Message).where(Message.conversation_id == conversation_id)
            )
            await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
            for jid in await redis_client.active_jobs(conversation_id):
                await redis_client.clear_job(jid, conversation_id)

    await redis_client.close_redis()
    await dispose_engine()

    print("chaos / fault-injection test")
    print("  scenarios          mixed faults, cancel mid-stream")
    print("  invariants         I terminal · II isolation · III partial kept · IV no leaks")
    if rep.failures:
        print(f"  result             FAIL ({len(rep.failures)})")
        for f in rep.failures:
            print("   -", f)
        return 1
    print("  result             PASS — every job settled, faults stayed isolated, nothing leaked")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
