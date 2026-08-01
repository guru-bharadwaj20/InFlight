import base64
import binascii
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

# Bounds nothing else in the stack enforces: there is no ASGI-level request
# body limit, so without these a single request can buffer an arbitrarily
# large string/attachment in memory before validation even runs, independent
# of max_concurrent_jobs_per_conversation (which only counts jobs, not bytes).
MAX_PROMPT_CHARS = 20_000
# ~4 MiB of decoded image bytes, expressed as the base64 length that produces it.
# Was 8 MiB each with up to 6 attachments, which let one request carry ~48 MiB of
# base64 — buffered whole, in memory, before validation ever ran. The composer
# downscales to 1600px and re-encodes as JPEG before upload, which lands far
# under this, so the cap only ever bites on a request that did not come from the
# app.
MAX_ATTACHMENT_BASE64_CHARS = 4 * 1024 * 1024 * 4 // 3
MAX_ATTACHMENTS = 4
# Belt to the per-field braces: caps the *total* across all attachments, so the
# worst case is one bound rather than the product of two.
MAX_TOTAL_ATTACHMENT_BASE64_CHARS = 8 * 1024 * 1024 * 4 // 3

# What the vision model is actually sent. An arbitrary string here reaches the
# provider as a declared content type, and nothing downstream re-derives it from
# the bytes.
ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "image/heic", "image/heif"}
)


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


# The passwords that actually get chosen and actually get guessed. A full
# breach-corpus check is the right long-term answer (and is what
# TRUST_PROXY_HEADERS-style config would gate), but a short embedded list
# removes the overwhelming majority of real-world weak choices for no
# dependency, no network call, and no latency.
_COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "password123", "passw0rd", "12345678",
        "123456789", "1234567890", "qwertyui", "qwerty123", "iloveyou",
        "sunshine", "princess", "football", "baseball", "welcome1",
        "admin123", "letmein1", "trustno1", "starwars", "whatever",
        "superman", "michael1", "dragon123", "monkey123", "abc12345",
        "changeme", "secret123", "welcome123", "p@ssw0rd", "qwertyuiop",
    }
)

# Eight identical characters clears min_length=8 while carrying almost no
# entropy. This is not a character-class rule -- those mostly teach people to
# append "1!" -- just a floor on variety.
_MIN_DISTINCT_CHARS = 5


def _strong_enough(value: str) -> str:
    """Reject the passwords that are weak in a way length cannot detect.

    min_length=8 was the only check, so "12345678", "password", and "aaaaaaaa"
    were all accepted -- and rate limiting slows an online guesser without
    helping at all if the password is the first thing anyone tries.
    """
    if value.lower() in _COMMON_PASSWORDS:
        raise ValueError(
            "this password is one of the most commonly used ones; pick something less guessable"
        )
    if len(set(value)) < _MIN_DISTINCT_CHARS:
        raise ValueError(
            f"password must use at least {_MIN_DISTINCT_CHARS} different characters"
        )
    if value.isdigit():
        raise ValueError("password must not be only digits")
    return value


NewPassword = Annotated[
    str,
    Field(min_length=8, max_length=BCRYPT_MAX_PASSWORD_BYTES),
    AfterValidator(_fits_bcrypt),
    AfterValidator(_strong_enough),
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


class WsTicketOut(BaseModel):
    """A single-use, short-lived credential for opening a WebSocket."""

    ticket: str
    expires_in: int


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

    @field_validator("mime_type")
    @classmethod
    def _known_image_type(cls, value: str) -> str:
        # Was a free-form string handed straight to the provider as a declared
        # content type. Parameters ("image/jpeg; charset=x") and case are both
        # legal in the wild, so normalise before matching rather than rejecting
        # a request that is really fine.
        normalised = value.split(";")[0].strip().lower()
        if normalised not in ALLOWED_IMAGE_MIME_TYPES:
            raise ValueError(
                f"unsupported attachment type {value!r}; expected one of "
                + ", ".join(sorted(ALLOWED_IMAGE_MIME_TYPES))
            )
        return normalised

    @field_validator("data")
    @classmethod
    def _decodable_base64(cls, value: str) -> str:
        # Validated here rather than in the job. base64.b64decode ran inside
        # run_job with no validate=, so a payload with bad padding raised
        # binascii.Error deep in the generation path and surfaced as a failed
        # answer with an opaque message -- for input the request handler could
        # have rejected outright with a 422.
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"attachment data is not valid base64: {exc}") from exc
        return value


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
    attachments: list[AttachmentIn] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS
    )

    @field_validator("attachments")
    @classmethod
    def _total_attachment_size(cls, value: list[AttachmentIn]) -> list[AttachmentIn]:
        total = sum(len(a.data) for a in value)
        if total > MAX_TOTAL_ATTACHMENT_BASE64_CHARS:
            raise ValueError(
                "attachments exceed the total size limit of "
                f"{MAX_TOTAL_ATTACHMENT_BASE64_CHARS} base64 characters"
            )
        return value


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
