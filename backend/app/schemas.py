from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
    prompt_message_id: str | None
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


class PromptCreate(BaseModel):
    content: str = Field(min_length=1)


class PromptAccepted(BaseModel):
    """Both rows a prompt creates: the committed prompt, and the job answering it.

    The assistant row is returned still `pending` — the answer itself arrives
    over the WebSocket, keyed by `assistant_message.id`.
    """

    user_message: MessageOut
    assistant_message: MessageOut


class ModelRate(BaseModel):
    """USD per 1M tokens."""

    input: float
    output: float


class PricingOut(BaseModel):
    updated: str
    source: str
    currency: str
    unit: str
    note: str
    models: dict[str, ModelRate]


class HealthOut(BaseModel):
    status: str
    postgres: str
    redis: str
    generation_model: str
    classifier_model: str
    gemini_key_configured: bool
