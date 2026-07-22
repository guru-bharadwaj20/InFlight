"""SQLAlchemy mapping over the tables Prisma migrates.

prisma/schema.prisma is authoritative. Nothing here creates or alters tables —
`Base.metadata.create_all` is deliberately never called. When the Prisma schema
changes, mirror it here and run backend/scripts/check_schema.py.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .ids import new_id


def utcnow() -> datetime:
    """Timestamps are minted in Python, not by the database.

    Every ordering decision in this system compares timestamps, so they all have
    to come from one clock. Letting some rows take a Postgres `now()` default and
    others a Python value would mix two clocks in the same comparison.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Role:
    USER = "user"
    ASSISTANT = "assistant"


class Status:
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"

    TERMINAL = frozenset({COMPLETE, ERROR, CANCELLED})
    ACTIVE = frozenset({PENDING, STREAMING})


class DependencyMode:
    AUTO = "auto"
    CHAINED = "chained"
    INDEPENDENT = "independent"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.submitted_at",
    )


class Message(Base):
    """A row here is a job, not just a bubble.

    `submitted_at` orders the list the user reads; `completed_at` orders the
    commits; `context_cutoff` is the read snapshot the job was stamped with.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default=Status.PENDING)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    dependency_mode: Mapped[str] = mapped_column(String, nullable=False, default=DependencyMode.AUTO)
    parent_message_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    prompt_message_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    parent: Mapped["Message | None"] = relationship(
        remote_side="Message.id",
        foreign_keys=[parent_message_id],
        back_populates="children",
    )
    children: Mapped[list["Message"]] = relationship(
        foreign_keys=[parent_message_id], back_populates="parent"
    )
    prompt: Mapped["Message | None"] = relationship(
        remote_side="Message.id",
        foreign_keys=[prompt_message_id],
        back_populates="answers",
    )
    answers: Mapped[list["Message"]] = relationship(
        foreign_keys=[prompt_message_id], back_populates="prompt"
    )

    __table_args__ = (
        Index("messages_conversation_id_submitted_at_idx", "conversation_id", "submitted_at"),
        Index("messages_conversation_id_completed_at_idx", "conversation_id", "completed_at"),
        Index("messages_conversation_id_status_idx", "conversation_id", "status"),
    )
