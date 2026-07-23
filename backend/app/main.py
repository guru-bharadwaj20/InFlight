import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import jobs, lifecycle, redis_client, scheduler
from .config import get_settings
from .db import dispose_engine, init_engine
from .logging_config import configure_logging, request_id_ctx
from .routers import auth, conversations, health, metrics, models, pricing, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    init_engine()
    redis_client.init_redis()
    # Listen for cross-worker control messages (cancellation) so a cancel that
    # lands on any worker reaches the one actually running the job.
    await jobs.start_control_listener()
    try:
        yield
    finally:
        # Drain first — let in-flight generations finish (and clients see a clean
        # done) before tearing down the services those jobs still depend on.
        await lifecycle.drain(settings.drain_timeout_seconds)
        await jobs.stop_control_listener()
        await scheduler.stop_scheduler()
        await redis_client.close_redis()
        await dispose_engine()


app = FastAPI(
    title="InFlight",
    version="0.1.0",
    description=(
        "Non-blocking concurrent chat: several prompts generate at once against "
        "one shared history, reconciled with MVCC-style snapshot isolation."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    """Assign (or accept) a request id and expose it, so every log line and the
    caller can be tied to the same request."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(pricing.router)
app.include_router(models.router)
app.include_router(metrics.router)
app.include_router(ws.router)


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    return {"service": "inflight-backend", "docs": "/docs", "health": "/health"}
