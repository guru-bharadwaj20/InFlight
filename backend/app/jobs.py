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
import base64
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import dependency, events, redis_client, scheduler, telemetry
from .config import get_settings
from .db import session_factory
from .dependency import Source, Verdict
from .ids import new_id
from .logging_config import job_id_ctx
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
# a reference to can be garbage-collected mid-generation. This registry is
# per-worker by design: it holds only the tasks *this* process is running, which
# is exactly the set this process is able to cancel directly.
_running: dict[str, asyncio.Task] = {}

# Identifies this worker in logs and control messages. Cancellation across
# workers does not depend on it — ownership is implicit in `_running` — but it
# makes the control plane observable.
WORKER_ID = new_id()

_control_task: asyncio.Task | None = None

# Speculative jobs and whether they have already been rolled back once, so a
# speculation is re-run at most a single time. Tracked in-process because the
# job's own task performs the retry, in the same worker that ran it.
_speculated: dict[str, bool] = {}


def spawn(job_id: str, conversation_id: str) -> asyncio.Task:
    task = asyncio.create_task(run_job(job_id, conversation_id), name=f"job:{job_id}")
    _running[job_id] = task
    # Only clear the registry entry if it still points at *this* task. A restart
    # (regenerate or a speculative rollback) replaces the entry with a new task
    # before the old one finishes; without this guard the old task's completion
    # would evict the new task and make it uncancellable.
    task.add_done_callback(
        lambda t: _running.pop(job_id, None) if _running.get(job_id) is t else None
    )
    return task


def is_running(job_id: str) -> bool:
    return job_id in _running


def cancel_local(job_id: str) -> bool:
    """Cancel a job this worker is running. False if it lives elsewhere (or not
    at all) — the caller then relies on the control broadcast to reach its owner."""
    task = _running.get(job_id)
    if task is None:
        return False
    task.cancel()
    return True


async def request_cancel(job_id: str) -> bool:
    """Ask whichever worker owns this job to cancel it.

    Tries the local registry first (the common single-worker case, and instant),
    then broadcasts on the control channel so a peer holding the task acts on it.
    Returns True if this worker owned and cancelled it directly; a False here does
    not mean the job will not be cancelled — only that its owner is another
    worker, which the broadcast reaches.
    """
    local = cancel_local(job_id)
    await redis_client.publish_control({"type": "cancel", "job_id": job_id, "by": WORKER_ID})
    return local


async def _control_loop() -> None:
    """Act on control broadcasts. Only the worker that owns a job in `_running`
    will actually cancel anything; for everyone else the message is a no-op."""
    async with redis_client.subscribe_control() as pubsub:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message["data"])
            except (ValueError, TypeError):
                continue
            if payload.get("type") == "cancel":
                job_id = payload.get("job_id")
                if job_id and job_id in _running:
                    logger.info("worker %s cancelling job %s on control signal", WORKER_ID, job_id)
                    cancel_local(job_id)


async def start_control_listener() -> None:
    global _control_task
    if _control_task is None:
        _control_task = asyncio.create_task(_control_loop(), name="control-listener")


async def stop_control_listener() -> None:
    global _control_task
    if _control_task is not None:
        _control_task.cancel()
        try:
            await _control_task
        except asyncio.CancelledError:
            pass
        _control_task = None


async def restart_job(
    session: AsyncSession,
    message: Message,
    conversation_id: str,
    *,
    reason: str,
) -> None:
    """Reset an answer row to pending against a fresh snapshot and re-run it.

    The abort/retry half of optimistic concurrency, shared by the user-initiated
    regenerate endpoint and the automatic speculative rollback. Re-stamping the
    cutoff to now is the whole fix — the prompt is unchanged, but everything that
    has committed since is inside the new snapshot.
    """
    message.status = Status.PENDING
    message.content = None
    message.error = None
    message.completed_at = None
    message.prompt_tokens = None
    message.completion_tokens = None
    message.context_cutoff = utcnow()
    message.stale_context_reason = None
    message.stale_context_source_id = None
    message.detected_dependency = Verdict.INDEPENDENT
    message.dependency_source = Source.HEURISTIC
    message.dependency_reason = reason
    await session.commit()
    await events.append(conversation_id, events.REGENERATED, message.id, reason=reason)

    await redis_client.set_job_state(
        message.id, status=Status.PENDING, conversation_id=conversation_id, seq=0
    )
    await redis_client.clear_buffer(message.id)
    await redis_client.register_active_job(conversation_id, message.id)
    spawn(message.id, conversation_id)


async def _deferred_restart(job_id: str, conversation_id: str, reason: str) -> None:
    """Re-run a job from its own new task, so the finishing job's task never
    clobbers the restarted one. Owns its session, like any job step."""
    async with session_factory()() as session:
        message = await session.get(Message, job_id)
        if message is not None:
            await restart_job(session, message, conversation_id, reason=reason)


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
            prompt_text = await _text_of(session, job.prompt_message_id)
            telemetry.CLASSIFIER_CALLS.inc()
            # The classifier call can retry with backoff, so it must not hold a
            # pooled DB connection idle-in-transaction for that whole time, and
            # it must go through the same admission gate as generation or a
            # burst of ambiguous prompts fires unbounded concurrent provider
            # calls with none of the scheduler's fairness/rate-limit guarantees.
            await session.commit()
            async with scheduler.get_scheduler().slot(f"classifier:{conversation_id}"):
                depends = await classify_dependency(prompt_text, texts)
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

    if settings.speculative_execution and job.dependency_mode == DependencyMode.AUTO:
        # Optimistic path: do not block. Proceed on the current snapshot and let
        # the retrospective check decide, after the fact, whether skipping the
        # wait cost real context. Marked INDEPENDENT so that check (which skips
        # jobs that waited) still considers this one; `_speculated` remembers the
        # truth so the outcome can be counted and rolled back at most once.
        _speculated[job.id] = False
        job.detected_dependency = Verdict.INDEPENDENT
        job.dependency_source = Source.HEURISTIC
        job.dependency_reason = "speculated: detection said dependent, proceeding optimistically"
        await session.commit()
        telemetry.SPECULATIONS.inc()
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
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.max_dependency_wait_seconds
    interval = settings.dependency_poll_interval_ms / 1000
    waited = False
    wait_start = loop.time()

    while await _earlier_in_flight(session, job):
        if loop.time() > deadline:
            logger.warning("job %s gave up waiting for predecessors", job.id)
            break
        waited = True
        await asyncio.sleep(interval)

    if waited:
        telemetry.WAIT_SECONDS.observe(loop.time() - wait_start)
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
        await events.append(
            conversation_id, events.STALE_FLAGGED, message.id,
            reason=message.stale_context_reason,
            source_id=message.stale_context_source_id,
        )
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

    # Append the terminal event to the append-only log before the frame goes out.
    if status == Status.COMPLETE:
        await events.append(
            conversation_id, events.COMPLETED, job.id,
            content=content, prompt_tokens=job.prompt_tokens,
            completion_tokens=job.completion_tokens,
        )
    elif status == Status.CANCELLED:
        await events.append(conversation_id, events.CANCELLED, job.id, content=content)
    else:
        await events.append(conversation_id, events.FAILED, job.id, error=error)

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
    # Drop the whole job record, not just its membership of the active set. The
    # replay buffer is only reachable through that set, so once the job leaves
    # it the buffer is unreadable and would otherwise sit in Redis until its TTL
    # expired. The terminal frame above already carried the final content, and
    # the row is committed, so nothing still needs it.
    await redis_client.clear_job(job.id, conversation_id)


async def run_job(job_id: str, conversation_id: str) -> None:
    usage = Usage()
    parts: list[str] = []
    seq = 0

    # Stamp this job's id onto every log line it produces (and it inherits the
    # spawning request's id from the copied context), so logs correlate with
    # /traces and with the request that started it.
    job_id_ctx.set(job_id)

    async with session_factory()() as session:
        job = await session.get(Message, job_id)
        if job is None:
            logger.warning("job %s vanished before it ran", job_id)
            await redis_client.unregister_active_job(conversation_id, job_id)
            return

        model = job.model
        trace = telemetry.begin_trace(job_id, conversation_id, model)
        telemetry.JOBS_ACTIVE.inc()
        try:
            # Before anything else: does this job have to wait for someone?
            with trace.span("resolve"):
                await _resolve_dependency(session, job, conversation_id)
            trace.note(job.detected_dependency, job.dependency_source)
            telemetry.record_verdict(job.detected_dependency, job.dependency_source)
            await events.append(
                conversation_id, events.RESOLVED, job_id,
                verdict=job.detected_dependency, source=job.dependency_source,
            )

            with trace.span("context"):
                turns = await build_context(session, job)

            # Images submitted with this prompt, stashed in Redis by the handler.
            # Decoded here rather than in the LLM layer so that stays free of any
            # transport concern.
            images = [
                (a["mime_type"], base64.b64decode(a["data"]))
                for a in await redis_client.get_attachments(job_id)
            ]

            # Fair admission: wait here (still pending) for a process-wide slot
            # and a rate-limit token before touching the model. Queued jobs are
            # served round-robin per conversation, so one chat's burst cannot
            # starve another's.
            with trace.span("await_slot"):
                slot = scheduler.get_scheduler().slot(conversation_id)
                slot_wait = await slot.__aenter__()
            telemetry.SLOT_WAIT_SECONDS.observe(slot_wait)
            try:
                job.status = Status.STREAMING
                await session.commit()
                await events.append(conversation_id, events.STREAMING, job_id)
                await redis_client.set_job_state(job_id, status=Status.STREAMING)
                await redis_client.publish(
                    conversation_id, job_id, {"type": "status", "status": Status.STREAMING}
                )

                gen_start = time.perf_counter()
                with trace.span("generate"):
                    async for text in stream_completion(turns, usage, model=model, images=images):
                        if seq == 0:  # first token: record time-to-first-token
                            telemetry.TTFT_SECONDS.observe(time.perf_counter() - gen_start)
                            trace.mark_ttft()
                        seq += 1
                        parts.append(text)
                        await redis_client.append_chunk(job_id, text, seq)
                        await redis_client.publish(
                            conversation_id, job_id, {"type": "chunk", "seq": seq, "text": text}
                        )
                telemetry.GENERATION_SECONDS.observe(time.perf_counter() - gen_start)
            finally:
                await slot.__aexit__(None, None, None)

            await _finish(
                session,
                job,
                conversation_id,
                status=Status.COMPLETE,
                content="".join(parts),
                usage=usage,
                model=model,
            )
            trace.finish("complete", usage.prompt_tokens, usage.completion_tokens)
            telemetry.JOBS_TOTAL.labels(outcome="complete").inc()
            # Advisory, and deliberately after the answer has been committed and
            # sent: a failure to spot a stale sibling must not fail the job that
            # just succeeded.
            try:
                await review_stale_context(session, conversation_id)
            except Exception:
                logger.exception("stale-context review failed for %s", conversation_id)

            # Resolve a speculation: if the retrospective check flagged this job,
            # the optimistic bet lost — roll back and re-run once against the
            # fuller snapshot. Otherwise the wait was correctly skipped.
            if job_id in _speculated:
                flagged = await session.execute(
                    select(Message.stale_context_reason).where(Message.id == job_id)
                )
                if flagged.scalar_one_or_none():
                    telemetry.SPECULATION_OUTCOME.labels(outcome="rolled_back").inc()
                    # From a detached task, so this finishing task cannot clobber
                    # the restarted one (the done-callback guard also protects it).
                    asyncio.create_task(
                        _deferred_restart(
                            job_id,
                            conversation_id,
                            "speculation rolled back: re-run with fuller context",
                        )
                    )
                else:
                    telemetry.SPECULATION_OUTCOME.labels(outcome="kept").inc()

        except asyncio.CancelledError:
            # Keep whatever was generated before the cancel — it is what the user
            # saw on screen, and discarding it would make the bubble jump.
            #
            # Shielded: `_finish` commits the row, appends the event, and
            # publishes the terminal frame in sequence. A second cancel arriving
            # while drain()'s timeout re-cancels a straggling task must not
            # interrupt that sequence partway — it would leave the row committed
            # as cancelled with no matching event/frame ever sent. The shield
            # lets this cleanup run to completion regardless of further cancels;
            # the CancelledError we're already handling still propagates after.
            await asyncio.shield(
                _finish(
                    session,
                    job,
                    conversation_id,
                    status=Status.CANCELLED,
                    content="".join(parts) or None,
                    error="cancelled",
                    usage=usage,
                )
            )
            trace.finish("cancelled", usage.prompt_tokens, usage.completion_tokens)
            telemetry.JOBS_TOTAL.labels(outcome="cancelled").inc()
            raise

        except LLMNotConfigured as exc:
            await _finish(
                session, job, conversation_id, status=Status.ERROR, error=str(exc)
            )
            trace.finish("error")
            telemetry.JOBS_TOTAL.labels(outcome="error").inc()

        except Exception as exc:
            # One job failing must not touch its siblings, so nothing is
            # re-raised past here.
            logger.exception("job %s failed", job_id)
            await _finish(
                session,
                job,
                conversation_id,
                status=Status.ERROR,
                error=describe_error(exc, model),
                usage=usage,
            )
            trace.finish("error", usage.prompt_tokens, usage.completion_tokens)
            telemetry.JOBS_TOTAL.labels(outcome="error").inc()
        finally:
            telemetry.JOBS_ACTIVE.dec()
            # The re-run (if any) is INDEPENDENT, so dropping the flag here caps
            # speculative rollback at exactly one and prevents the map growing.
            _speculated.pop(job_id, None)


__all__ = [
    "spawn",
    "request_cancel",
    "cancel_local",
    "is_running",
    "run_job",
    "build_context",
    "start_control_listener",
    "stop_control_listener",
    "WORKER_ID",
]
