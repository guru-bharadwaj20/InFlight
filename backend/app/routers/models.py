"""The Gemini models this key can generate chat text with.

Queried live from the provider rather than hardcoded, so the picker reflects
whatever the account actually has — new releases appear without a code change.
Cached in memory for a while: the list changes rarely and the call, while cheap,
should not run on every page load.
"""

import asyncio
import logging
import time

from fastapi import APIRouter

from ..config import get_settings
from ..llm import LLMNotConfigured, get_client
from ..schemas import ModelInfo, ModelsOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])

# Substrings that mark a model as not a text-chat model: speech, image
# generation, embeddings, and the like. Excluded so the picker only offers
# things that can actually answer a prompt.
_EXCLUDE = ("tts", "image", "robotics", "embedding", "aqa", "-customtools")

_CACHE_TTL = 600  # seconds
# How long to sit on a failure before trying the provider again. Short enough
# that a blip self-heals quickly, long enough that a sustained outage is not
# amplified into one upstream call per page load.
_ERROR_CACHE_TTL = 30
_cache: tuple[float, list[ModelInfo]] | None = None
# Set when the last attempt failed, so the TTL check knows which window applies.
_cache_is_fallback = False
# Only one caller talks to the provider at a time; the rest wait for that result
# instead of each opening their own request on a cold or just-expired cache.
_fetch_lock = asyncio.Lock()


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


def _fresh(now: float) -> bool:
    if _cache is None:
        return False
    ttl = _ERROR_CACHE_TTL if _cache_is_fallback else _CACHE_TTL
    return now - _cache[0] < ttl


@router.get("/models", response_model=ModelsOut)
async def list_models() -> ModelsOut:
    """The chat models this key can use.

    Unauthenticated, so the caching here is load-bearing rather than a nicety:
    every miss is an outbound provider call that anyone can trigger.
    """
    global _cache, _cache_is_fallback
    default = get_settings().generation_model

    if _fresh(time.monotonic()):
        return ModelsOut(default=default, models=_cache[1])

    async with _fetch_lock:
        # Re-check under the lock: while waiting, whoever held it may have
        # already refreshed, and a queue of callers should not each go on to
        # repeat the same upstream call.
        now = time.monotonic()
        if _fresh(now):
            return ModelsOut(default=default, models=_cache[1])

        fallback = [ModelInfo(id=default, label=default)]
        try:
            models, _cache_is_fallback = await _fetch(), False
        except LLMNotConfigured:
            # No key: offer just the configured default so the picker still renders.
            models, _cache_is_fallback = fallback, True
        except Exception:
            # Any other provider failure used to propagate as a 500 *and* cache
            # nothing, so an unauthenticated caller could drive one upstream
            # models.list per request for as long as the provider stayed unwell.
            # Serve the last good list if there is one, else the configured
            # default, and cache that briefly so the failure is absorbed.
            logger.exception("listing models failed; serving a cached or default list")
            models = _cache[1] if _cache is not None else fallback
            _cache_is_fallback = True

        _cache = (now, models)
        return ModelsOut(default=default, models=models)
