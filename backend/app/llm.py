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

from .config import get_settings
from .models import Role

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are a helpful assistant in a chat application. Answer the user's most "
    "recent message directly and concisely. Use Markdown for structure only when "
    "it genuinely helps."
)


class LLMNotConfigured(RuntimeError):
    """Raised when no API key is set, so the job can report it as a normal error."""


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

    try:
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
        return bool(json.loads(response.text)["depends_on_prior"])
    except Exception:
        logger.exception("dependency classifier failed; assuming dependent")
        return True


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
) -> AsyncIterator[str]:
    """Stream a completion for `turns`, recording token counts into `usage`."""
    if get_settings().use_fake_llm:
        async for text in _stream_fake(turns, usage):
            yield text
        return

    model = model or get_settings().generation_model

    stream = await get_client().aio.models.generate_content_stream(
        model=model,
        contents=to_contents(turns),
        config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
    )

    async for chunk in stream:
        # Usage is reported cumulatively, and only on some chunks, so the last
        # one carrying it holds the final counts.
        if chunk.usage_metadata is not None:
            if chunk.usage_metadata.prompt_token_count is not None:
                usage.prompt_tokens = chunk.usage_metadata.prompt_token_count
            if chunk.usage_metadata.candidates_token_count is not None:
                usage.completion_tokens = chunk.usage_metadata.candidates_token_count

        if chunk.text:
            yield chunk.text
