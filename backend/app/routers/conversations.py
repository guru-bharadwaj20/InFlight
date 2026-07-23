import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import dependency, events, jobs, redis_client, telemetry, tree
from ..auth import current_user
from ..config import Settings, get_settings
from ..db import get_session
from ..ids import new_id
from ..models import Conversation, DependencyMode, Message, Role, Status, User, utcnow
from ..ordering import order_for_display
from ..schemas import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    PromptAccepted,
    PromptCreate,
    StreamState,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _load_conversation(
    session: AsyncSession, conversation_id: str, user: User
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    # A conversation owned by someone else is reported as missing, not
    # forbidden: 403 would confirm the id exists to a user who should not know.
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conversation


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> Conversation:
    conversation = Conversation(title=payload.title, user_id=user.id)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> list[Conversation]:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        # Pinned chats first, then most recent — the order the sidebar renders.
        .order_by(Conversation.starred.desc(), Conversation.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> Conversation:
    """Rename or (un)star a chat — the sidebar's per-chat actions."""
    conversation = await _load_conversation(session, conversation_id, user)
    if payload.title is not None:
        conversation.title = payload.title.strip() or None
    if payload.starred is not None:
        conversation.starred = payload.starred
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    conversation = await _load_conversation(session, conversation_id, user)
    # Cancel anything still generating so no task keeps writing to a Redis key
    # for a conversation that no longer exists.
    for job_id in await redis_client.active_jobs(conversation_id):
        await jobs.request_cancel(job_id)  # reaches the owning worker, wherever it is
        await redis_client.clear_job(job_id, conversation_id)
    await session.delete(conversation)  # messages cascade via the FK
    await session.commit()


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ConversationDetailOut:
    conversation = await _load_conversation(session, conversation_id, user)

    # Display order is submitted_at — the order the user experienced, which is
    # not the order these rows completed in. Sorted in Python rather than SQL
    # because the key is the *exchange's* timestamp, which for an answer means
    # its prompt's: see ordering.order_for_display.
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.submitted_at.asc())
    )
    messages = order_for_display(list(result.scalars()))

    return ConversationDetailOut(
        id=conversation.id,
        title=conversation.title,
        starred=conversation.starred,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=PromptAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_prompt(
    conversation_id: str,
    payload: PromptCreate,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PromptAccepted:
    """Commit the prompt, then hand its answer to a background job.

    This returns as soon as both rows exist — it does not wait for the model. The
    answer arrives over the WebSocket, addressed by the assistant row's id. That
    is what makes the input lock in Stage 2 a purely client-side choice: the
    server is already willing to accept the next prompt immediately, and Stage 3
    removes the lock without touching this handler.
    """
    await _load_conversation(session, conversation_id, user)

    # Idempotency: two sends carrying the same key are one intent. The first
    # claims the key and creates the job; a duplicate returns the first result
    # instead of spawning a second. Scoped to the user so keys cannot collide
    # across accounts.
    idem = f"{user.id}:{idempotency_key}" if idempotency_key else None
    if idem and not await redis_client.claim_idempotency(idem):
        replay = await _idempotent_replay(session, idem)
        if replay is not None:
            return replay
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a request with this Idempotency-Key is still being processed",
        )

    if payload.parent_message_id:
        parent = await session.get(Message, payload.parent_message_id)
        if parent is None or parent.conversation_id != conversation_id:
            if idem:
                await redis_client.release_idempotency(idem)
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "parent_message_id must name a message in this conversation",
            )

    # The slot is claimed before any row is written, and the id is minted here so
    # there is something to claim it with. Checking a count and then inserting
    # would admit every one of N simultaneous sends, since they all read the
    # same count before any of them registered.
    job_id = new_id()
    if not await redis_client.reserve_active_job(
        conversation_id, job_id, settings.max_concurrent_jobs_per_conversation
    ):
        if idem:
            await redis_client.release_idempotency(idem)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"already at the limit of {settings.max_concurrent_jobs_per_conversation} "
            "concurrent answers for this conversation",
        )

    submitted_at = utcnow()
    user_message = Message(
        conversation_id=conversation_id,
        role=Role.USER,
        content=payload.content,
        status=Status.COMPLETE,
        submitted_at=submitted_at,
        completed_at=submitted_at,
        context_cutoff=submitted_at,
    )

    # The cutoff has to land strictly after the prompt commits, or the job would
    # not see the very message it is answering. Deriving it from the prompt's own
    # timestamp rather than reading the clock twice makes that ordering exact:
    # two `utcnow()` calls in quick succession can return the same value on a
    # coarse system clock, and `completed_at < cutoff` is a strict comparison.
    cutoff = submitted_at + timedelta(microseconds=1)

    if payload.parent_message_id:
        # An explicit chain is a deterministic override, so detection is not
        # consulted at all. This is the escape hatch for when the heuristic and
        # the classifier are both wrong, which is why it must not depend on
        # either of them being right.
        mode = DependencyMode.CHAINED
        verdict, source, reason = (
            dependency.Verdict.DEPENDENT,
            dependency.Source.CHAINED,
            "chained to an earlier message by the user",
        )
    else:
        mode = DependencyMode.AUTO
        detection = dependency.evaluate(payload.content)
        verdict, source, reason = (
            detection.verdict,
            dependency.Source.HEURISTIC,
            detection.reason,
        )

    assistant_message = Message(
        id=job_id,
        conversation_id=conversation_id,
        role=Role.ASSISTANT,
        content=None,
        status=Status.PENDING,
        submitted_at=cutoff,
        context_cutoff=cutoff,
        model=payload.model or settings.generation_model,
        dependency_mode=mode,
        parent_message_id=payload.parent_message_id,
        detected_dependency=verdict,
        dependency_source=source,
        dependency_reason=reason,
    )

    try:
        session.add(user_message)
        await session.flush()  # assigns the prompt id the job needs to point at
        assistant_message.prompt_message_id = user_message.id
        session.add(assistant_message)
        await session.commit()
    except Exception:
        # The slot was claimed before the rows existed, so a failed write must
        # give it back or the conversation leaks capacity until the TTL expires.
        await redis_client.unregister_active_job(conversation_id, job_id)
        if idem:
            await redis_client.release_idempotency(idem)
        raise

    # Set before returning so the WebSocket's resume list sees the job the moment
    # this responds, rather than only once the task happens to be scheduled.
    await redis_client.set_job_state(
        job_id, status=Status.PENDING, conversation_id=conversation_id, seq=0
    )
    # Stash any images before spawning, so the task finds them when it reads its
    # own prompt. Written under the assistant row's id, which is the job id.
    if payload.attachments:
        await redis_client.set_attachments(
            job_id, [a.model_dump() for a in payload.attachments]
        )
    jobs.spawn(job_id, conversation_id)
    telemetry.PROMPTS_SUBMITTED.inc()
    await events.append(
        conversation_id, events.SUBMITTED, job_id,
        model=assistant_message.model, verdict=verdict, source=source,
    )

    if idem:
        # Record the result so a duplicate returns exactly these two rows.
        await redis_client.store_idempotency(
            idem, json.dumps({"u": user_message.id, "a": assistant_message.id})
        )

    return PromptAccepted(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
    )


async def _idempotent_replay(session: AsyncSession, idem: str) -> PromptAccepted | None:
    """Return the result a prior identical submission produced, or None if the
    original is still in flight (claimed but not yet committed)."""
    raw = await redis_client.get_idempotency(idem)
    if not raw or raw == "pending":
        # The original is mid-creation; give it a moment to commit its rows.
        for _ in range(20):
            await asyncio.sleep(0.05)
            raw = await redis_client.get_idempotency(idem)
            if raw and raw != "pending":
                break
        else:
            return None
    ids = json.loads(raw)
    user_msg = await session.get(Message, ids["u"])
    asst_msg = await session.get(Message, ids["a"])
    if user_msg is None or asst_msg is None:
        return None
    return PromptAccepted(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(asst_msg),
    )


@router.post(
    "/{conversation_id}/messages/{message_id}/regenerate",
    response_model=MessageOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate(
    conversation_id: str,
    message_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> Message:
    """Re-run one answer against a fresh snapshot.

    The abort/retry half of the optimistic-concurrency pattern: the job was
    allowed to proceed on the assumption it was independent, and this is what
    that assumption costs when it turns out to be wrong. Re-stamping the cutoff
    to now is the whole fix — the prompt is unchanged, but everything that has
    committed since is now inside its snapshot.

    User-initiated only. The retrospective check suggests; it never re-runs.
    """
    await _load_conversation(session, conversation_id, user)

    message = await session.get(Message, message_id)
    if message is None or message.conversation_id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    if message.role != Role.ASSISTANT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only answers can be regenerated")
    if message.status not in Status.TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "this answer is still generating")

    await jobs.restart_job(
        session,
        message,
        conversation_id,
        reason="regenerated against a fresh snapshot",
    )
    return message


@router.post(
    "/{conversation_id}/messages/{message_id}/cancel",
    response_model=MessageOut,
)
async def cancel_message(
    conversation_id: str,
    message_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> Message:
    """Stop one in-flight answer, keeping whatever it had already produced.

    Cancelling is per job by construction — sibling jobs share nothing but the
    conversation, so stopping one cannot disturb the others.
    """
    await _load_conversation(session, conversation_id, user)

    message = await session.get(Message, message_id)
    if message is None or message.conversation_id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    if message.status in Status.TERMINAL:
        return message

    # Ask whichever worker owns the job to cancel it. In a single-worker deploy
    # this cancels locally and instantly; across workers it broadcasts, and the
    # owner settles the row and emits the cancelled frame through the normal path.
    await jobs.request_cancel(message_id)

    # Give the owner a moment to settle it, then check the authoritative state.
    settled = await _await_terminal(session, message_id, timeout=1.5)
    if not settled:
        # Nobody owns the task — it was orphaned by a restart, so the row would
        # sit "streaming" forever. Settle it here rather than leave it stuck.
        message.status = Status.CANCELLED
        message.error = "cancelled"
        await session.commit()
        await redis_client.clear_job(message_id, conversation_id)
        await redis_client.publish(
            conversation_id,
            message_id,
            {
                "type": "error",
                "status": Status.CANCELLED,
                "content": message.content,
                "error": "cancelled",
                "completed_at": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "model": message.model,
            },
        )

    await session.refresh(message)
    return message


async def _await_terminal(
    session: AsyncSession, message_id: str, *, timeout: float
) -> bool:
    """Poll the authoritative row until it reaches a terminal state or times out.

    Scalar column reads, so the owner's commit (made in another session, possibly
    another worker) is observed rather than a stale identity-map copy.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await session.execute(
            select(Message.status).where(Message.id == message_id)
        )
        current = result.scalar_one_or_none()
        if current is None or current in Status.TERMINAL:
            return True
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)


@router.get(
    "/{conversation_id}/messages/{message_id}/stream",
    response_model=StreamState,
)
async def stream_state(
    conversation_id: str,
    message_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> StreamState:
    """The authoritative text-so-far for one job, for resyncing after a gap.

    The streaming protocol is at-least-once: a client drops any chunk it has
    already seen (`seq` <= its cursor) and, on spotting a *forward* gap (a chunk
    whose seq skips ahead), fetches this instead of blindly appending — which is
    what turns a lossy fan-out into exactly-once *text*. While the job streams,
    this returns the live Redis replay buffer; once it has committed, the buffer
    is gone and this returns the final answer straight from the row.
    """
    await _load_conversation(session, conversation_id, user)

    message = await session.get(Message, message_id)
    if message is None or message.conversation_id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")

    state, buffer = await redis_client.job_snapshot(message_id)
    if state:
        return StreamState(
            status=state.get("status") or message.status,
            text=buffer,
            seq=int(state.get("seq") or 0),
            final=False,
        )

    # No live buffer: the job already committed (or never had one). The row is
    # authoritative, and its content is final — signal that so the client stops
    # expecting more chunks and ignores any late duplicates.
    return StreamState(
        status=message.status,
        text=message.content or "",
        seq=0,
        final=message.status in Status.TERMINAL,
    )


@router.get("/{conversation_id}/tree")
async def conversation_tree(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """The conversation as a DAG: messages nested under their branch parent.

    Linear chats return a single spine; a forked one returns the branches. The
    edge is `parent_message_id`, the same edge chaining already sets.
    """
    await _load_conversation(session, conversation_id, user)
    result = await session.execute(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    messages = list(result.scalars())
    return {"roots": tree.build_tree(messages)}


@router.get("/{conversation_id}/thread/{message_id}")
async def conversation_thread(
    conversation_id: str,
    message_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """One branch in isolation: the root-to-leaf path ending at `message_id`."""
    await _load_conversation(session, conversation_id, user)
    result = await session.execute(
        select(Message).where(Message.conversation_id == conversation_id)
    )
    messages = list(result.scalars())
    return {"thread": tree.thread_to(messages, message_id)}


@router.get("/{conversation_id}/events")
async def conversation_events(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> dict:
    """The append-only event log for this conversation, oldest first.

    Every job lifecycle transition is here as an immutable event. `projected` is
    the read model rebuilt purely from those events — it exists to show the
    `messages` table is a projection of this log, not an independent source of
    truth.
    """
    await _load_conversation(session, conversation_id, user)
    log = await events.read(conversation_id)
    return {"count": len(log), "events": log, "projected": events.project(log)}


@router.get("/{conversation_id}/context", response_model=list[MessageOut])
async def get_context_snapshot(
    conversation_id: str,
    at: datetime | None = Query(
        None, description="Snapshot instant (ISO 8601). Defaults to now."
    ),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> list[Message]:
    """The context a job stamped with `at` would see.

    This is the read side of the snapshot rule made inspectable: only messages
    that *completed* strictly before the cutoff are visible, regardless of when
    they were submitted. A prompt submitted earlier but still streaming is
    correctly absent.
    """
    await _load_conversation(session, conversation_id, user)
    cutoff = at or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == Status.COMPLETE,
            Message.completed_at.is_not(None),
            Message.completed_at < cutoff,
        )
        .order_by(Message.completed_at.asc())
    )
    return list(result.scalars())


__all__ = ["router", "Role"]
