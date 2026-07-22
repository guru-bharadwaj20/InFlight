"""One generation job: read the snapshot, stream a completion, commit it.

Stage 2 only ever has one of these running, because the UI locks the input while
a response streams. Nothing in this file assumes that. A job reads the history
exactly once, at the cutoff it was stamped with, and never re-reads it — which
is the property that lets Stage 3 run several at once without any of them
observing another's partial writes.

The job owns its own database session. It outlives the HTTP request that spawned
it, so it cannot borrow that request's session.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import redis_client
from .db import session_factory
from .llm import LLMNotConfigured, Turn, Usage, stream_completion
from .models import Message, Status, utcnow

logger = logging.getLogger(__name__)

# asyncio only holds a weak reference to a running task, so a task nobody keeps
# a reference to can be garbage-collected mid-generation.
_running: dict[str, asyncio.Task] = {}


def spawn(job_id: str, conversation_id: str) -> asyncio.Task:
    task = asyncio.create_task(run_job(job_id, conversation_id), name=f"job:{job_id}")
    _running[job_id] = task
    task.add_done_callback(lambda _: _running.pop(job_id, None))
    return task


def is_running(job_id: str) -> bool:
    return job_id in _running


async def cancel(job_id: str) -> bool:
    task = _running.get(job_id)
    if task is None:
        return False
    task.cancel()
    return True


async def build_context(session: AsyncSession, job: Message) -> list[Turn]:
    """The transcript this job is allowed to read.

    Only rows that *completed* strictly before the cutoff are visible, ordered by
    when they committed rather than when they were submitted. A prompt submitted
    earlier but still streaming is correctly absent — it hasn't committed, so as
    far as this job is concerned it does not exist.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == job.conversation_id,
            Message.status == Status.COMPLETE,
            Message.completed_at.is_not(None),
            Message.completed_at < job.context_cutoff,
        )
        .order_by(Message.completed_at.asc())
    )
    return [Turn(role=m.role, content=m.content or "") for m in result.scalars()]


async def _finish(
    session: AsyncSession,
    job: Message,
    conversation_id: str,
    *,
    status: str,
    content: str | None = None,
    error: str | None = None,
    usage: Usage | None = None,
    model: str | None = None,
) -> None:
    """Commit the job's terminal state and tell the client about it.

    Postgres is written before the frame goes out, so a client that reacts to the
    frame by re-fetching the conversation cannot read a row that still says
    "streaming".
    """
    job.status = status
    job.content = content
    job.error = error
    job.completed_at = utcnow() if status == Status.COMPLETE else None
    if usage is not None:
        job.prompt_tokens = usage.prompt_tokens
        job.completion_tokens = usage.completion_tokens
    if model is not None:
        job.model = model

    await session.commit()

    await redis_client.publish(
        conversation_id,
        job.id,
        {
            "type": "done" if status == Status.COMPLETE else "error",
            "status": status,
            "content": content,
            "error": error,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "prompt_tokens": job.prompt_tokens,
            "completion_tokens": job.completion_tokens,
            "model": job.model,
        },
    )
    await redis_client.unregister_active_job(conversation_id, job.id)


async def run_job(job_id: str, conversation_id: str) -> None:
    usage = Usage()
    parts: list[str] = []
    seq = 0

    async with session_factory()() as session:
        job = await session.get(Message, job_id)
        if job is None:
            logger.warning("job %s vanished before it ran", job_id)
            await redis_client.unregister_active_job(conversation_id, job_id)
            return

        model = job.model
        try:
            turns = await build_context(session, job)

            job.status = Status.STREAMING
            await session.commit()
            await redis_client.set_job_state(job_id, status=Status.STREAMING)
            await redis_client.publish(
                conversation_id, job_id, {"type": "status", "status": Status.STREAMING}
            )

            async for text in stream_completion(turns, usage, model=model):
                seq += 1
                parts.append(text)
                await redis_client.append_chunk(job_id, text, seq)
                await redis_client.publish(
                    conversation_id, job_id, {"type": "chunk", "seq": seq, "text": text}
                )

            await _finish(
                session,
                job,
                conversation_id,
                status=Status.COMPLETE,
                content="".join(parts),
                usage=usage,
                model=model,
            )

        except asyncio.CancelledError:
            # Keep whatever was generated before the cancel — it is what the user
            # saw on screen, and discarding it would make the bubble jump.
            await _finish(
                session,
                job,
                conversation_id,
                status=Status.CANCELLED,
                content="".join(parts) or None,
                error="cancelled",
                usage=usage,
            )
            raise

        except LLMNotConfigured as exc:
            await _finish(
                session, job, conversation_id, status=Status.ERROR, error=str(exc)
            )

        except Exception as exc:
            # One job failing must not touch its siblings, so nothing is
            # re-raised past here.
            logger.exception("job %s failed", job_id)
            await _finish(
                session,
                job,
                conversation_id,
                status=Status.ERROR,
                error=f"{exc.__class__.__name__}: {exc}",
                usage=usage,
            )


__all__ = ["spawn", "cancel", "is_running", "run_job", "build_context"]
