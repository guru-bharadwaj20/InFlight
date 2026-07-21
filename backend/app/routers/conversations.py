from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Conversation, Message, Role, Status
from ..schemas import (
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    MessageCreate,
    MessageOut,
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
    # not the order these rows completed in.
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.submitted_at.asc())
    )
    messages = list(result.scalars())

    return ConversationDetailOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_message(
    conversation_id: str,
    payload: MessageCreate,
    session: AsyncSession = Depends(get_session),
) -> Message:
    """Write a message row directly.

    Stage 1 has no generation path, so this just persists a row — enough to
    exercise the schema end to end. Stage 2 replaces the body with "insert the
    user row, then spawn the assistant job".
    """
    await _load_conversation(session, conversation_id)

    now = datetime.now(timezone.utc)
    message = Message(
        conversation_id=conversation_id,
        role=payload.role,
        content=payload.content,
        status=payload.status,
        submitted_at=now,
        # The snapshot is taken at submit time: this job may only read messages
        # that had already committed by this instant.
        context_cutoff=now,
        completed_at=now if payload.status == Status.COMPLETE else None,
        dependency_mode=payload.dependency_mode,
        parent_message_id=payload.parent_message_id,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return message


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
