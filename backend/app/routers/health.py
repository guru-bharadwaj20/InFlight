from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import redis_client
from ..config import Settings, get_settings
from ..db import get_session
from ..schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HealthOut:
    try:
        await session.execute(text("select 1"))
        postgres = "ok"
    except Exception as exc:  # surfaced in the UI, so keep the reason
        postgres = f"error: {exc.__class__.__name__}"

    try:
        redis_status = "ok" if await redis_client.ping() else "error: no pong"
    except Exception as exc:
        redis_status = f"error: {exc.__class__.__name__}"

    return HealthOut(
        status="ok" if postgres == "ok" and redis_status == "ok" else "degraded",
        postgres=postgres,
        redis=redis_status,
        generation_model=settings.generation_model,
        classifier_model=settings.classifier_model,
        anthropic_key_configured=bool(settings.anthropic_api_key),
    )
