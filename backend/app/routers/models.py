"""The Gemini models this key can generate chat text with.

Queried live from the provider rather than hardcoded, so the picker reflects
whatever the account actually has — new releases appear without a code change.
Cached in memory for a while: the list changes rarely and the call, while cheap,
should not run on every page load.
"""

import time

from fastapi import APIRouter

from ..config import get_settings
from ..llm import LLMNotConfigured, get_client
from ..schemas import ModelInfo, ModelsOut

router = APIRouter(tags=["models"])

# Substrings that mark a model as not a text-chat model: speech, image
# generation, embeddings, and the like. Excluded so the picker only offers
# things that can actually answer a prompt.
_EXCLUDE = ("tts", "image", "robotics", "embedding", "aqa", "-customtools")

_CACHE_TTL = 600  # seconds
_cache: tuple[float, list[ModelInfo]] | None = None


def _is_chat_model(name: str, methods) -> bool:
    if "gemini" not in name:
        return False
    if methods and "generateContent" not in methods:
        return False
    return not any(bad in name for bad in _EXCLUDE)


async def _fetch() -> list[ModelInfo]:
    client = get_client()
    out: list[ModelInfo] = []
    async for m in await client.aio.models.list(config={"query_base": True}):
        name = m.name or ""
        methods = (
            getattr(m, "supported_actions", None)
            or getattr(m, "supported_generation_methods", None)
        )
        if not _is_chat_model(name, methods):
            continue
        out.append(
            ModelInfo(
                id=name.removeprefix("models/"),
                label=getattr(m, "display_name", None) or name.removeprefix("models/"),
            )
        )
    # Newest first reads best in a dropdown; the id sorts descending well enough
    # (3.6 > 3.5 > 2.5 ...), with "latest" aliases floated to the top.
    out.sort(key=lambda x: (not x.id.endswith("latest"), x.id), reverse=False)
    out.sort(key=lambda x: x.id, reverse=True)
    return out


@router.get("/models", response_model=ModelsOut)
async def list_models() -> ModelsOut:
    global _cache
    default = get_settings().generation_model

    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return ModelsOut(default=default, models=_cache[1])

    try:
        models = await _fetch()
    except LLMNotConfigured:
        # No key: offer just the configured default so the picker still renders.
        models = [ModelInfo(id=default, label=default)]
    _cache = (now, models)
    return ModelsOut(default=default, models=models)
