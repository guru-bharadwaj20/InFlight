"""Fixed-window rate limiting, backed by Redis so it holds across workers.

Nothing in this app was throttled. `/auth/login` in particular was an unmetered
password oracle: guesses could be fired as fast as the network allowed, against
an endpoint that also does a bcrypt hash per attempt. That second part made it
worse than a normal brute-force target, because every guess cost the *server*
50-200ms of CPU — so the same traffic that hunts a password is also a denial of
service, and before the hashing moved off the event loop it stalled every
in-flight generation in the worker while it ran.

Fixed window rather than a sliding log: one INCR and one EXPIRE per request, no
per-request set to store or trim. A fixed window lets a caller land up to 2x the
limit across a boundary, which for "slow down a guesser" is an irrelevant amount
of slack and not worth the extra state.

Counters live in Redis, not in process memory, so the limit is the limit no
matter which worker a request reaches — a per-process counter would multiply the
real allowance by the number of replicas.

Failure policy is fail-open, deliberately: if Redis is unreachable this allows
the request and logs. Redis being down already breaks generation, and turning
that outage into "nobody can log in either" makes an incident worse for no
security gain — an attacker cannot cause the outage by guessing passwords.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status

from . import redis_client
from .config import Settings, get_settings

logger = logging.getLogger(__name__)


def client_key(request: Request, settings: Settings) -> str:
    """Best-effort caller identity for limiting.

    X-Forwarded-For is only honoured when TRUST_PROXY_HEADERS says a proxy is
    actually in front of this process. Trusting it unconditionally would make the
    limit trivially bypassable — the header is attacker-controlled, so a guesser
    could simply vary it and get a fresh budget per request.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Left-most entry is the original client; the rest are proxies.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def hit(bucket: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Count one request against `bucket`. Returns (allowed, seconds_until_reset)."""
    if limit <= 0:
        return True, 0
    key = f"ratelimit:{bucket}"
    try:
        client = redis_client.get_redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            # Only meaningful on the first hit of a window; on later hits the key
            # already has a TTL and NX leaves it alone, so the window does not
            # slide forward under sustained traffic (which would let a steady
            # attacker keep it alive and never reset).
            pipe.expire(key, window_seconds, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
    except Exception:
        logger.exception("rate limit check failed for %s; allowing the request", bucket)
        return True, 0

    return int(count) <= limit, max(int(ttl), 0)


def limit_by_client(name: str, limit: int, window_seconds: int):
    """Dependency limiting a route by caller address."""

    async def dependency(
        request: Request, settings: Settings = Depends(get_settings)
    ) -> None:
        allowed, retry_after = await hit(
            f"{name}:{client_key(request, settings)}", limit, window_seconds
        )
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many requests — wait a moment and try again",
                headers={"Retry-After": str(retry_after or window_seconds)},
            )

    return dependency


async def limit_login(request: Request, email: str, settings: Settings) -> None:
    """Limit login by address *and* by the account being targeted.

    Two buckets, because either alone is easy to walk around. Limiting only by
    address lets a distributed attempt spread one account's guesses across many
    hosts; limiting only by account lets one host spray a whole list of accounts
    at full speed. Requiring both keeps each cheap and closes both shapes.
    """
    for bucket, limit in (
        (f"login:ip:{client_key(request, settings)}", settings.login_rate_limit_per_ip),
        (f"login:account:{email.lower()}", settings.login_rate_limit_per_account),
    ):
        allowed, retry_after = await hit(bucket, limit, settings.login_rate_window_seconds)
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many sign-in attempts — wait a moment and try again",
                headers={
                    "Retry-After": str(retry_after or settings.login_rate_window_seconds)
                },
            )
