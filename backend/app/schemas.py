from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str


class TokenOut(BaseModel):
    token: str
    user: UserOut


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
    stale_context_reason: str | None
    stale_context_source_id: str | None
    detected_dependency: str | None
    dependency_source: str | None
    dependency_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    model: str | None
    error: str | None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    starred: bool
    created_at: datetime
    updated_at: datetime


class ConversationUpdate(BaseModel):
    """Partial edit from the sidebar: rename, or toggle the star. Both optional."""

    title: str | None = None
    starred: bool | None = None


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    title: str | None = None


class PromptCreate(BaseModel):
    content: str = Field(min_length=1)
    # Set to chain this prompt to an earlier message: a deterministic override
    # that makes the job wait, whatever dependency detection would have said.
    parent_message_id: str | None = None


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
