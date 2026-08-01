from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

# Bounds nothing else in the stack enforces: there is no ASGI-level request
# body limit, so without these a single request can buffer an arbitrarily
# large string/attachment in memory before validation even runs, independent
# of max_concurrent_jobs_per_conversation (which only counts jobs, not bytes).
MAX_PROMPT_CHARS = 20_000
# ~8 MiB of decoded image bytes, expressed as the base64 length that produces it.
MAX_ATTACHMENT_BASE64_CHARS = 8 * 1024 * 1024 * 4 // 3


# bcrypt refuses (and historically silently truncated) anything past 72 *bytes*.
# Bounding the field by characters is not the same constraint: "é" is one
# character and two bytes, so a 72-character accented password is 144 bytes and
# reached security.hash_password's ValueError as an unhandled 500. Validating the
# encoded length here turns that into the 422 it always should have been, and
# keeps the API's stated limit and the hasher's real limit the same number.
BCRYPT_MAX_PASSWORD_BYTES = 72


def _fits_bcrypt(value: str) -> str:
    if len(value.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes "
            "(non-ASCII characters count for more than one)"
        )
    return value


NewPassword = Annotated[
    str,
    Field(min_length=8, max_length=BCRYPT_MAX_PASSWORD_BYTES),
    AfterValidator(_fits_bcrypt),
]
# Not `NewPassword`: an existing account may predate a tightened signup rule, and
# the login form's job is to check a password, not to re-impose policy on one
# that was already accepted.
ExistingPassword = Annotated[
    str,
    Field(min_length=1, max_length=BCRYPT_MAX_PASSWORD_BYTES),
    AfterValidator(_fits_bcrypt),
]


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: NewPassword


class LoginIn(BaseModel):
    email: EmailStr
    password: ExistingPassword


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


def _not_blank(value: str) -> str:
    # min_length=1 admits "   ", which every downstream consumer then strips back
    # to nothing: to_contents drops the turn, the transcript comes out empty, and
    # the provider rejects the call with an opaque error rendered in the bubble.
    # Reject it at the edge, where the message can actually be useful.
    if not value.strip():
        raise ValueError("content must contain more than whitespace")
    return value


class PromptCreate(BaseModel):
    content: Annotated[
        str,
        Field(min_length=1, max_length=MAX_PROMPT_CHARS),
        AfterValidator(_not_blank),
    ]
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
