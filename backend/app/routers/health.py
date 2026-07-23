from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import lifecycle, redis_client
from ..config import Settings, get_settings
from ..db import get_session
from ..schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict:
    """Is the process up? Answering at all is the answer. A failing liveness
    probe means restart me; it must not depend on any external service."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Should this worker get traffic right now? False while draining or while a
    dependency is unreachable, so a load balancer routes around it."""
    if lifecycle.is_draining():
        return JSONResponse(status_code=503, content={"status": "draining"})

    checks = {}
    ok = True
    try:
        await session.execute(text("select 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc.__class__.__name__}"
        ok = False
    try:
        checks["redis"] = "ok" if await redis_client.ping() else "error: no pong"
        ok = ok and checks["redis"] == "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
        ok = False

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", **checks},
    )


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
        gemini_key_configured=bool(settings.gemini_api_key),
    )
