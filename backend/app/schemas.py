from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Bounds nothing else in the stack enforces: there is no ASGI-level request
# body limit, so without these a single request can buffer an arbitrarily
# large string/attachment in memory before validation even runs, independent
# of max_concurrent_jobs_per_conversation (which only counts jobs, not bytes).
MAX_PROMPT_CHARS = 20_000
# ~8 MiB of decoded image bytes, expressed as the base64 length that produces it.
MAX_ATTACHMENT_BASE64_CHARS = 8 * 1024 * 1024 * 4 // 3


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


class ModelInfo(BaseModel):
    id: str
    label: str


class ModelsOut(BaseModel):
    default: str
    models: list[ModelInfo]


class AttachmentIn(BaseModel):
    """One image sent with a prompt: a MIME type and base64-encoded bytes."""

    mime_type: str
    data: str = Field(max_length=MAX_ATTACHMENT_BASE64_CHARS)


class PromptCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    # Set to chain this prompt to an earlier message: a deterministic override
    # that makes the job wait, whatever dependency detection would have said.
    parent_message_id: str | None = None
    # The model to answer with, chosen in the composer. Falls back to the
    # configured default when omitted.
    model: str | None = None
    # Images attached in the composer, passed to the vision model for this
    # prompt only. Capped to keep an oversized request from wedging a job.
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=6)


class StreamState(BaseModel):
    """The authoritative text-so-far for one job, for a client to resync against.

    Fetched when the client detects a gap in the chunk sequence (a frame it never
    received). `seq` is how many chunks this text covers, so the client can reset
    to it exactly and resume dropping anything it has already seen.
    """

    status: str
    text: str
    seq: int
    # True once the job has committed and its replay buffer has been dropped, so
    # `text` here is the final answer from the row, not a live buffer.
    final: bool = False


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
