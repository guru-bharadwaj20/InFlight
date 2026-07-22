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

from . import dependency, redis_client
from .config import get_settings
from .db import session_factory
from .dependency import Source, Verdict
from .llm import (
    LLMNotConfigured,
    Turn,
    Usage,
    classify_dependency,
    describe_error,
    stream_completion,
)
from .models import DependencyMode, Message, Role, Status, utcnow

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
    """The transcript this job is allowed to read: settled exchanges, then its own prompt.

    Only rows that *completed* strictly before the cutoff are visible, in commit
    order rather than submission order. But visibility alone isn't the whole
    rule, because user rows commit the instant they are submitted: at the moment
    a job takes its snapshot, the conversation can already contain sibling
    prompts that were fired concurrently and have no answers yet.

    Including those would be actively wrong — the model would read them as part
    of the question it is being asked and try to answer all of them at once. So
    context is assembled as *answered pairs*: an exchange enters the transcript
    only when both halves have committed. A prompt still waiting on its answer is
    invisible, exactly like the answer itself.

    This job's own prompt is then appended last, which is what makes it the
    question rather than another piece of history.
    """
    answered = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == job.conversation_id,
            Message.role == Role.ASSISTANT,
            Message.status == Status.COMPLETE,
            Message.completed_at.is_not(None),
            Message.completed_at < job.context_cutoff,
        )
        .order_by(Message.completed_at.asc())
        # A job that waited for a predecessor may already hold that row from
        # before it finished, when its content was still null. Without this the
        # WHERE clause would correctly select it and the identity map would
        # then hand back the stale, empty version.
        .execution_options(populate_existing=True)
    )
    answers = list(answered.scalars())

    prompt_ids = {a.prompt_message_id for a in answers if a.prompt_message_id}
    if job.prompt_message_id:
        prompt_ids.add(job.prompt_message_id)

    prompts: dict[str, Message] = {}
    if prompt_ids:
        found = await session.execute(
            select(Message)
            .where(Message.id.in_(prompt_ids))
            .execution_options(populate_existing=True)
        )
        prompts = {m.id: m for m in found.scalars()}

    turns: list[Turn] = []
    for answer in answers:
        prompt = prompts.get(answer.prompt_message_id or "")
        if prompt is not None:
            turns.append(Turn(role=Role.USER, content=prompt.content or ""))
        turns.append(Turn(role=Role.ASSISTANT, content=answer.content or ""))

    own_prompt = prompts.get(job.prompt_message_id or "")
    if own_prompt is not None:
        turns.append(Turn(role=Role.USER, content=own_prompt.content or ""))

    return turns


async def _earlier_in_flight(session: AsyncSession, job: Message) -> list[str]:
    """Prompt ids of jobs submitted before this one that have not settled yet.

    Strictly earlier, which is what makes waiting safe: a job only ever waits on
    jobs ahead of it in submission order, so no cycle can form and two mutually
    dependent prompts cannot deadlock each other.

    Selects plain columns rather than ORM entities deliberately. This runs in a
    poll loop against rows *other* tasks are mutating, and loading entities would
    put them in this session's identity map, where their stale attribute values
    would then be handed back on every later read.
    """
    result = await session.execute(
        select(Message.prompt_message_id)
        .where(
            Message.conversation_id == job.conversation_id,
            Message.role == Role.ASSISTANT,
            Message.id != job.id,
            Message.submitted_at < job.submitted_at,
            Message.status.in_(tuple(Status.ACTIVE)),
        )
        .order_by(Message.submitted_at.asc())
    )
    return [row[0] for row in result.all() if row[0]]


async def _text_of(session: AsyncSession, message_id: str | None) -> str:
    if not message_id:
        return ""
    result = await session.execute(
        select(Message.content).where(Message.id == message_id)
    )
    return result.scalar_one_or_none() or ""


async def _chain_target(session: AsyncSession, job: Message) -> str | None:
    """The message a chained job must actually wait for.

    Chaining to an assistant bubble means waiting for that answer. Chaining to a
    *question* means waiting for the answer to it — the user's intent is the same
    either way, and the prompt row itself is already complete, so waiting on it
    would return instantly and silently do nothing.
    """
    parent_id = job.parent_message_id
    if not parent_id:
        return None

    result = await session.execute(select(Message.role).where(Message.id == parent_id))
    role = result.scalar_one_or_none()
    if role != Role.USER:
        return parent_id

    answer = await session.execute(
        select(Message.id).where(Message.prompt_message_id == parent_id)
    )
    return answer.scalars().first() or parent_id


async def _await_settled(
    session: AsyncSession, message_id: str | None, settings
) -> None:
    """Block until one specific message reaches a terminal state."""
    if not message_id:
        return

    deadline = asyncio.get_running_loop().time() + settings.max_dependency_wait_seconds
    interval = settings.dependency_poll_interval_ms / 1000

    while True:
        result = await session.execute(
            select(Message.status).where(Message.id == message_id)
        )
        status = result.scalar_one_or_none()
        if status is None or status in Status.TERMINAL:
            return
        if asyncio.get_running_loop().time() > deadline:
            logger.warning("chained job gave up waiting for %s", message_id)
            return
        await asyncio.sleep(interval)


async def _resolve_dependency(
    session: AsyncSession, job: Message, conversation_id: str
) -> None:
    """Settle whether this job must wait, then wait for as long as it says.

    Runs inside the job rather than in the request handler on purpose. Sending
    must stay instant even for a prompt that turns out to be dependent — the
    waiting belongs to the job, not to the user's keystroke.
    """
    settings = get_settings()
    verdict = job.detected_dependency

    if job.dependency_mode == DependencyMode.CHAINED:
        # Deterministic override: wait for exactly the message the user pointed
        # at, and skip detection entirely. This is the safety net for when the
        # heuristic and classifier are both wrong, so it cannot be routed
        # through either of them.
        await _await_settled(session, await _chain_target(session, job), settings)
        job.context_cutoff = utcnow()
        await session.commit()
        return

    if verdict == Verdict.UNSURE:
        pending = await _earlier_in_flight(session, job)
        if not pending:
            # Nothing is in flight, so there is nothing this prompt could be
            # waiting on. No need to spend a classifier call to learn that.
            job.detected_dependency = Verdict.INDEPENDENT
            job.dependency_reason = "nothing in flight to depend on"
        else:
            texts = [await _text_of(session, pid) for pid in pending]
            depends = await classify_dependency(
                await _text_of(session, job.prompt_message_id), texts
            )
            job.detected_dependency = (
                Verdict.DEPENDENT if depends else Verdict.INDEPENDENT
            )
            job.dependency_source = Source.CLASSIFIER
            job.dependency_reason = (
                "classifier: needs the earlier answer"
                if depends
                else "classifier: self-contained"
            )
        verdict = job.detected_dependency
        await session.commit()
        await redis_client.publish(
            conversation_id,
            job.id,
            {
                "type": "dependency",
                "detected_dependency": job.detected_dependency,
                "dependency_source": job.dependency_source,
                "dependency_reason": job.dependency_reason,
            },
        )

    if verdict != Verdict.DEPENDENT:
        return

    deadline = asyncio.get_running_loop().time() + settings.max_dependency_wait_seconds
    interval = settings.dependency_poll_interval_ms / 1000
    waited = False

    while await _earlier_in_flight(session, job):
        if asyncio.get_running_loop().time() > deadline:
            logger.warning("job %s gave up waiting for predecessors", job.id)
            break
        waited = True
        await asyncio.sleep(interval)

    if waited:
        # The whole point of waiting was to see what landed while we waited, so
        # the snapshot has to be retaken. Keeping the original cutoff would mean
        # blocking for an answer and then ignoring it.
        job.context_cutoff = utcnow()
        await session.commit()


async def review_stale_context(session: AsyncSession, conversation_id: str) -> None:
    """Look for answers that ran without context they turned out to need.

    Runs after every completion, over the whole conversation, because the pair
    only becomes checkable once *both* halves are done — and which of the two
    finishes last is exactly what is unpredictable here. Scanning both
    directions each time is cheaper than tracking who is waiting on whom.

    A pair is suspicious when the later prompt could not see the earlier answer
    (its cutoff predates that answer's commit), it was not made to wait, and the
    retrospective check finds a reference in it that the unseen answer would
    have resolved.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == Role.ASSISTANT,
            Message.status == Status.COMPLETE,
        )
        .order_by(Message.completed_at.asc())
        .execution_options(populate_existing=True)
    )
    answers = list(result.scalars())
    if len(answers) < 2:
        return

    prompts: dict[str, str] = {}
    ids = [a.prompt_message_id for a in answers if a.prompt_message_id]
    if ids:
        rows = await session.execute(
            select(Message.id, Message.content).where(Message.id.in_(ids))
        )
        prompts = {r[0]: r[1] or "" for r in rows.all()}

    flagged = []
    for later in answers:
        if later.stale_context_reason or later.detected_dependency == Verdict.DEPENDENT:
            continue
        prompt = prompts.get(later.prompt_message_id or "")
        if not prompt:
            continue

        for earlier in answers:
            if earlier.id == later.id or not earlier.completed_at:
                continue
            # Did `later` start before `earlier` committed, and finish after?
            if earlier.completed_at <= later.context_cutoff:
                continue
            if earlier.submitted_at > later.submitted_at:
                continue

            reason = dependency.retrospective_conflict(prompt, earlier.content or "")
            if reason:
                later.stale_context_reason = reason
                later.stale_context_source_id = earlier.id
                flagged.append(later)
                break

    if not flagged:
        return

    await session.commit()
    for message in flagged:
        await redis_client.publish(
            conversation_id,
            message.id,
            {
                "type": "stale_context",
                "stale_context_reason": message.stale_context_reason,
                "stale_context_source_id": message.stale_context_source_id,
            },
        )


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
            # Before anything else: does this job have to wait for someone?
            await _resolve_dependency(session, job, conversation_id)

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
            # Advisory, and deliberately after the answer has been committed and
            # sent: a failure to spot a stale sibling must not fail the job that
            # just succeeded.
            try:
                await review_stale_context(session, conversation_id)
            except Exception:
                logger.exception("stale-context review failed for %s", conversation_id)

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
                error=describe_error(exc),
                usage=usage,
            )


__all__ = ["spawn", "cancel", "is_running", "run_job", "build_context"]
