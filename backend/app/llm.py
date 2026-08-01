"""Gemini generation, wrapped so nothing else in the app imports the SDK.

The orchestration layer only ever needs two things from a provider: given an
ordered transcript, yield text as it arrives, and afterwards report token usage.
Keeping the surface that small is what keeps the concurrency work in jobs.py
provider-agnostic — swapping models (or vendors) touches this file only.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache

from google import genai
from google.genai import types

from . import resilience, telemetry
from .config import get_settings
from .models import Role
from .resilience import CircuitBreaker, CircuitOpen

logger = logging.getLogger(__name__)

# One breaker guards every provider call in this worker. Its state feeds the
# CIRCUIT_STATE gauge so an open circuit is visible in /metrics.
_STATE_CODE = {"closed": 0, "half_open": 1, "open": 2}


@lru_cache
def _breaker() -> CircuitBreaker:
    s = get_settings()
    cb = CircuitBreaker(s.circuit_breaker_threshold, s.circuit_breaker_cooldown_seconds)

    def _reflect(state) -> None:
        telemetry.CIRCUIT_STATE.set(_STATE_CODE.get(state.value, 0))
        if state.value == "open":
            telemetry.CIRCUIT_TRIPS.inc()

    cb.on_state_change = _reflect
    return cb

SYSTEM_INSTRUCTION = (
    "You are a helpful assistant in a chat application. Answer the user's most "
    "recent message directly and concisely. Use Markdown for structure only when "
    "it genuinely helps."
)


class LLMNotConfigured(RuntimeError):
    """Raised when no API key is set, so the job can report it as a normal error."""


class EmptyTranscript(RuntimeError):
    """Raised when a snapshot normalises down to nothing to send.

    `to_contents` drops blank turns and pops a leading model turn, so a transcript
    can legitimately come out empty. Sending that produces an opaque provider
    rejection; naming the condition here makes the bubble say something true."""


def describe_error(exc: BaseException, model: str | None = None) -> str:
    """Turn a provider exception into something worth showing in a chat bubble.

    The SDK raises with the whole error envelope embedded in the message —
    nested JSON, quota metric names, help URLs. Dropping that verbatim into the
    conversation tells the user nothing they can act on, so the cases that have
    an actual remedy get told what it is. Anything unrecognised still surfaces,
    truncated, rather than being swallowed.
    """
    text = str(exc)
    # The job's own model, not the configured one: a regenerated row can still
    # carry the model it was first created with, and naming the wrong one sends
    # the reader to the wrong setting.
    model = model or get_settings().generation_model

    # Type checks come first. What an exception *is* outranks what its message
    # happens to spell: these branches used to sit below the substring tests, so
    # any breaker or empty-transcript error whose text merely contained "429" or
    # "blocked" would have been described as something it was not.
    if isinstance(exc, CircuitOpen):
        return (
            "The model provider is failing repeatedly, so requests are paused "
            "briefly to let it recover. Try again in a moment."
        )
    if isinstance(exc, EmptyTranscript):
        return (
            "There was no prompt content to send to the model. Try sending the "
            "message again with some text."
        )

    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return (
            f"Rate limit reached for {model}. The free tier allows only a small "
            "number of requests per day, per model. Wait for the quota to reset, "
            "set GENERATION_MODEL to a model with headroom, or run with "
            "USE_FAKE_LLM=true to exercise the app without the provider."
        )
    if "UNAVAILABLE" in text or "503" in text:
        return "The model is temporarily unavailable upstream. Try again shortly."
    if "PERMISSION_DENIED" in text or "API key not valid" in text or "401" in text:
        return "The API key was rejected. Check GEMINI_API_KEY in .env."
    if "NOT_FOUND" in text or "404" in text:
        return (
            f"Model '{model}' was not found. Check GENERATION_MODEL names a "
            "model your key can reach."
        )
    if "SAFETY" in text or "blocked" in text.lower():
        return "The provider blocked this response under its safety filters."

    # Anything unrecognised: name the exception type and stop there.
    #
    # This used to append 200 characters of the raw exception, which for this SDK
    # is the provider's whole error envelope — nested JSON carrying project ids,
    # quota metric names, internal endpoints and help URLs. That text was written
    # to the messages table and rendered verbatim in the chat bubble, so an
    # upstream misconfiguration leaked deployment details to whoever happened to
    # be chatting. It also told the user nothing they could act on.
    #
    # The full text is not lost: run_job already logs the exception with its
    # traceback, which is where an operator should be reading it from anyway.
    logger.warning("unrecognised provider error surfaced to a user: %s", type(exc).__name__)
    return (
        f"The model call failed ({type(exc).__name__}). The details are in the "
        "server logs."
    )


@dataclass
class Turn:
    """One committed message from the snapshot, on its way into a prompt."""

    role: str
    content: str


@dataclass
class Usage:
    """Filled in by `stream_completion` as the stream progresses.

    An async generator can't return a value to its caller, and the token counts
    only arrive with the last chunks, so the caller passes one of these in and
    reads it once the stream is exhausted.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@lru_cache
def get_client() -> genai.Client:
    api_key = get_settings().gemini_api_key
    if not api_key:
        raise LLMNotConfigured("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def to_contents(turns: Sequence[Turn]) -> list[types.Content]:
    """Turn a snapshot into the alternating transcript Gemini expects.

    Two shapes have to be normalised away, both of which this system produces
    legitimately. Adjacent same-role turns happen whenever an assistant job
    errored or was cancelled — its row never commits, so the two user prompts
    around it end up neighbours. A leading model turn is the same situation at
    the head of the conversation.
    """
    merged: list[tuple[str, list[str]]] = []

    for turn in turns:
        text = (turn.content or "").strip()
        if not text:
            continue
        role = "model" if turn.role == Role.ASSISTANT else "user"
        if merged and merged[-1][0] == role:
            merged[-1][1].append(text)
        else:
            merged.append((role, [text]))

    while merged and merged[0][0] == "model":
        merged.pop(0)

    return [
        types.Content(role=role, parts=[types.Part.from_text(text="\n\n".join(texts))])
        for role, texts in merged
    ]


DEPENDENCY_SYSTEM = (
    "You decide whether a new chat message needs the answer to an earlier, still-"
    "unfinished message in order to be answered correctly.\n\n"
    "Answer true only if the new message cannot be properly answered without "
    "knowing what the earlier answer said — for example it refers back to it, "
    "builds on it, or asks to modify it. Answer false if the new message is "
    "self-contained, even when it is on a related topic. A pronoun that resolves "
    "inside the new message itself does not make it dependent."
)

DEPENDENCY_SCHEMA = {
    "type": "OBJECT",
    "properties": {"depends_on_prior": {"type": "BOOLEAN"}},
    "required": ["depends_on_prior"],
}


async def classify_dependency(
    prompt: str, in_flight: Sequence[str], model: str | None = None
) -> bool:
    """Does `prompt` need one of the still-generating `in_flight` prompts answered first?

    Reached only for prompts the heuristic could not settle, so the latency is
    paid on a minority of sends. Constrained decoding rather than prose parsing —
    the model is given a response schema and can only return the one boolean.

    A failure here returns True: if we cannot tell, waiting costs latency while
    guessing independence costs a wrong answer.
    """
    try:
        return await classify_dependency_strict(prompt, in_flight, model)
    except Exception:
        # Indistinguishable from a real provider outage in the logs alone — a
        # programming bug in the classifier path (e.g. a malformed schema
        # response) reads identically to a 429/503 unless something separates
        # them. This counter is that separation.
        telemetry.CLASSIFIER_FAILURES.inc()
        logger.exception("dependency classifier failed; assuming dependent")
        return True


async def classify_dependency_strict(
    prompt: str, in_flight: Sequence[str], model: str | None = None
) -> bool:
    """As `classify_dependency`, but lets provider errors propagate.

    The eval harness needs this. Folding a quota rejection into "dependent"
    scores an outage as a judgement, which is how you end up publishing a
    classifier accuracy that is really an error rate.
    """
    settings = get_settings()

    if settings.use_fake_llm:
        # The offline path does not pretend to judge. It always allows
        # concurrency, which keeps it deterministic and exercises the optimistic
        # branch — the one whose failure mode Stage 9's retrospective check and
        # regenerate nudge exist to catch.
        return False

    earlier = "\n".join(f"- {p[:200]}" for p in in_flight) or "- (none)"
    question = (
        f"Earlier messages still being answered:\n{earlier}\n\n"
        f"New message:\n- {prompt[:500]}"
    )

    async def call() -> str:
        response = await get_client().aio.models.generate_content(
            model=model or settings.classifier_model,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=question)])],
            config=types.GenerateContentConfig(
                system_instruction=DEPENDENCY_SYSTEM,
                response_mime_type="application/json",
                response_schema=DEPENDENCY_SCHEMA,
                temperature=0,
            ),
        )
        return response.text

    text = await _with_retry(call)
    return bool(json.loads(text)["depends_on_prior"])


async def _with_retry(call):
    """Run a provider coroutine through the breaker, retrying transient failures.

    Safe only for idempotent, non-streaming calls (the classifier) — a stream
    that has emitted tokens must not be retried, which the streaming path handles
    itself.
    """
    settings = get_settings()
    breaker = _breaker()
    attempt = 0
    while True:
        attempt += 1
        if not breaker.allow():
            raise CircuitOpen("provider circuit is open")
        try:
            result = await call()
            breaker.record_success()
            return result
        except Exception as exc:
            breaker.record_failure()
            if resilience.is_transient(exc) and attempt <= settings.provider_max_retries:
                telemetry.PROVIDER_RETRIES.inc()
                await asyncio.sleep(resilience.backoff(attempt, settings.provider_retry_base_seconds))
                continue
            raise


FAKE_FAIL_MARKER = "[[fail]]"

FAKE_FILLER = (
    "This is a deterministic stand-in response used to exercise the "
    "orchestration layer without spending tokens on a real provider. "
)


async def _stream_fake(
    turns: Sequence[Turn], usage: Usage
) -> AsyncIterator[str]:
    """A local generator that streams like the real one, minus the network.

    Answer length scales with the prompt, so a deliberately wordy prompt takes
    visibly longer to finish than a terse one. That is what makes out-of-order
    completion demonstrable: the ordering the bubbles resolve in stops matching
    the order they were submitted.
    """
    prompt = turns[-1].content if turns else ""

    # Fault injection for the resilience tests. Provider failures are otherwise
    # only reproducible by actually breaking the provider, and per-job failure
    # isolation is precisely the property that needs a deterministic test.
    if FAKE_FAIL_MARKER in prompt:
        raise RuntimeError("injected provider failure")

    words = FAKE_FILLER.split()
    count = max(12, min(len(prompt.split()) * 6, 160))
    delay = get_settings().fake_llm_chunk_delay_ms / 1000

    usage.prompt_tokens = sum(len(t.content.split()) for t in turns)
    usage.completion_tokens = 0

    yield f"[fake] Re: {prompt[:60]}\n\n"
    for i in range(count):
        await asyncio.sleep(delay)
        usage.completion_tokens = i + 1
        yield words[i % len(words)] + " "


async def stream_completion(
    turns: Sequence[Turn],
    usage: Usage,
    model: str | None = None,
    images: Sequence[tuple[str, bytes]] = (),
) -> AsyncIterator[str]:
    """Stream a completion for `turns`, recording token counts into `usage`.

    `images` are attachments on the newest prompt (a Gemini vision call); they
    are added as parts on the final user turn so the model reads them alongside
    the question they were sent with.
    """
    if get_settings().use_fake_llm:
        async for text in _stream_fake(turns, usage):
            yield text
        return

    model = model or get_settings().generation_model

    contents = to_contents(turns)
    if images:
        image_parts = [types.Part.from_bytes(data=data, mime_type=mime) for mime, data in images]
        if contents and contents[-1].role == "user":
            contents[-1].parts = list(contents[-1].parts or []) + image_parts
        else:
            contents.append(types.Content(role="user", parts=image_parts))

    if not contents:
        # Nothing survived normalisation and no images made up for it. Fail here
        # with a legible reason rather than letting the provider reject an empty
        # request and surfacing its envelope in the chat bubble.
        raise EmptyTranscript("there was no prompt content to send to the model")

    settings = get_settings()
    breaker = _breaker()
    attempt = 0
    while True:
        attempt += 1
        if not breaker.allow():
            raise CircuitOpen("provider circuit is open")
        emitted = 0
        try:
            stream = await get_client().aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
            )
            async for chunk in stream:
                # Usage is reported cumulatively, and only on some chunks, so the
                # last one carrying it holds the final counts.
                if chunk.usage_metadata is not None:
                    if chunk.usage_metadata.prompt_token_count is not None:
                        usage.prompt_tokens = chunk.usage_metadata.prompt_token_count
                    if chunk.usage_metadata.candidates_token_count is not None:
                        usage.completion_tokens = chunk.usage_metadata.candidates_token_count
                if chunk.text:
                    emitted += 1
                    yield chunk.text
            breaker.record_success()
            return
        except Exception as exc:
            breaker.record_failure()
            # Retry only if nothing was emitted — once tokens have streamed, a
            # retry would duplicate text, so the failure must surface instead.
            if (
                emitted == 0
                and resilience.is_transient(exc)
                and attempt <= settings.provider_max_retries
            ):
                telemetry.PROVIDER_RETRIES.inc()
                await asyncio.sleep(resilience.backoff(attempt, settings.provider_retry_base_seconds))
                continue
            raise
