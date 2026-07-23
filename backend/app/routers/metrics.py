"""Observability endpoints: Prometheus metrics and recent job traces.

    curl http://localhost:8000/metrics          # Prometheus scrape target
    curl http://localhost:8000/traces | jq .     # last N job lifecycles

`/metrics` is the standard scrape endpoint (point Prometheus at it, or read it
by eye). `/traces` returns the in-process ring buffer of per-job spans — the
lifecycle of each recent generation, newest first — for walking a single
request's path without standing up a tracing backend.
"""

from fastapi import APIRouter, Query
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from .. import telemetry

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/traces")
async def traces(limit: int = Query(50, ge=1, le=200)) -> dict:
    recent = telemetry.recent_traces(limit)
    return {"count": len(recent), "traces": recent}
