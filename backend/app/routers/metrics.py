"""Observability endpoints: Prometheus metrics and recent job traces.

    curl http://localhost:8000/metrics          # Prometheus scrape target
    curl http://localhost:8000/traces | jq .     # last N job lifecycles

`/metrics` is the standard scrape endpoint (point Prometheus at it, or read it
by eye). `/traces` returns the in-process ring buffer of per-job spans — the
lifecycle of each recent generation, newest first — for walking a single
request's path without standing up a tracing backend.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from .. import scheduler, telemetry
from ..auth import optional_user
from ..config import Settings, get_settings
from ..models import User

router = APIRouter(tags=["observability"])


async def _observability_access(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    user: User | None = Depends(optional_user),
) -> None:
    """Gate the observability endpoints.

    These were wide open. They carry no message text, but they do expose job and
    conversation ids, models, token counts and per-generation timings for every
    recent job across every user — enough to profile who is using the system and
    how much, and to enumerate conversation ids, from an unauthenticated request.

    Two ways in: a shared METRICS_TOKEN (what a Prometheus scraper should use,
    since it has no user account), or any signed-in user. With no token
    configured only the second applies, so the endpoints are never simply public.
    """
    expected = settings.metrics_token
    if expected:
        scheme, _, presented = (authorization or "").partition(" ")
        # Constant-time: a plain == leaks the shared secret one byte at a time to
        # anyone willing to time the responses.
        if scheme.lower() == "bearer" and hmac.compare_digest(presented, expected):
            return

    if user is not None:
        return

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "observability endpoints require METRICS_TOKEN or a signed-in user",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get("/metrics", dependencies=[Depends(_observability_access)])
async def metrics() -> Response:
    # Sample the scheduler's queue depth at scrape time — a point-in-time gauge
    # is most honest read exactly when it is reported.
    telemetry.SCHEDULER_WAITING.set(scheduler.get_scheduler().waiting())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/traces", dependencies=[Depends(_observability_access)])
async def traces(limit: int = Query(50, ge=1, le=200)) -> dict:
    recent = telemetry.recent_traces(limit)
    return {"count": len(recent), "traces": recent}
