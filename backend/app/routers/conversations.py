from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import dependency, jobs, redis_client
from ..config import Settings, get_settings
from ..db import get_session
from ..models import Conversation, DependencyMode, Message, Role, Status, utcnow
from ..ordering import order_for_display
from ..schemas import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    MessageOut,
    PromptAccepted,
    PromptCreate,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _load_conversation(session: AsyncSession, conversation_id: str) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conversation


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_session),
) -> Conversation:
    conversation = Conversation(title=payload.title)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[Conversation]:
    result = await session.execute(
        select(Conversation).order_by(Conversation.created_at.desc()).limit(limit)
    )
    return list(result.scalars())


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
) -> ConversationDetailOut:
    conversation = await _load_conversation(session, conversation_id)

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
) -> PromptAccepted:
    """Commit the prompt, then hand its answer to a background job.

    This returns as soon as both rows exist — it does not wait for the model. The
    answer arrives over the WebSocket, addressed by the assistant row's id. That
    is what makes the input lock in Stage 2 a purely client-side choice: the
    server is already willing to accept the next prompt immediately, and Stage 3
    removes the lock without touching this handler.
    """
    await _load_conversation(session, conversation_id)

    if payload.parent_message_id:
        parent = await session.get(Message, payload.parent_message_id)
        if parent is None or parent.conversation_id != conversation_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "parent_message_id must name a message in this conversation",
            )

    in_flight = await redis_client.active_job_count(conversation_id)
    if in_flight >= settings.max_concurrent_jobs_per_conversation:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"{in_flight} jobs already in flight for this conversation",
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
        conversation_id=conversation_id,
        role=Role.ASSISTANT,
        content=None,
        status=Status.PENDING,
        submitted_at=cutoff,
        context_cutoff=cutoff,
        model=settings.generation_model,
        dependency_mode=mode,
        parent_message_id=payload.parent_message_id,
        detected_dependency=verdict,
        dependency_source=source,
        dependency_reason=reason,
    )

    session.add(user_message)
    await session.flush()  # assigns the prompt id the job needs to point at
    assistant_message.prompt_message_id = user_message.id
    session.add(assistant_message)
    await session.commit()

    # Registered here rather than inside the task so that the count above and the
    # WebSocket's resume list both see the job the moment this returns, instead
    # of only once the task happens to be scheduled.
    await redis_client.set_job_state(
        assistant_message.id,
        status=Status.PENDING,
        conversation_id=conversation_id,
        seq=0,
    )
    await redis_client.register_active_job(conversation_id, assistant_message.id)
    jobs.spawn(assistant_message.id, conversation_id)

    return PromptAccepted(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
    )


@router.get("/{conversation_id}/context", response_model=list[MessageOut])
async def get_context_snapshot(
    conversation_id: str,
    at: datetime | None = Query(
        None, description="Snapshot instant (ISO 8601). Defaults to now."
    ),
    session: AsyncSession = Depends(get_session),
) -> list[Message]:
    """The context a job stamped with `at` would see.

    This is the read side of the snapshot rule made inspectable: only messages
    that *completed* strictly before the cutoff are visible, regardless of when
    they were submitted. A prompt submitted earlier but still streaming is
    correctly absent.
    """
    await _load_conversation(session, conversation_id)
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
