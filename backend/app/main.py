from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import redis_client
from .config import get_settings
from .db import dispose_engine, init_engine
from .routers import auth, conversations, health, pricing, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    redis_client.init_redis()
    try:
        yield
    finally:
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

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(pricing.router)
app.include_router(ws.router)


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    return {"service": "inflight-backend", "docs": "/docs", "health": "/health"}
