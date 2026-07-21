from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import DependencyMode, Role, Status


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str | None
    status: str
    submitted_at: datetime
    completed_at: datetime | None
    context_cutoff: datetime
    dependency_mode: str
    parent_message_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    model: str | None
    error: str | None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    title: str | None = None


class MessageCreate(BaseModel):
    """Stage 1 only writes rows; Stage 2 gives this endpoint a generation path."""

    content: str = Field(min_length=1)
    role: str = Role.USER
    status: str = Status.COMPLETE
    dependency_mode: str = DependencyMode.AUTO
    parent_message_id: str | None = None


class HealthOut(BaseModel):
    status: str
    postgres: str
    redis: str
    generation_model: str
    classifier_model: str
    anthropic_key_configured: bool
