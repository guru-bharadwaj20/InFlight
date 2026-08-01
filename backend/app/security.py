"""Password hashing and signed session tokens.

Kept separate from the auth router so the hashing and token scheme can be tested
and swapped without touching request handling. bcrypt for passwords (slow by
design, salted per hash), a short JWT for the session.
"""

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from anyio.to_thread import run_sync

from .config import get_settings

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    # bcrypt silently truncates at 72 bytes; rejecting longer input is clearer
    # than hashing a prefix and letting a shortened password still authenticate.
    if len(plain.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # A malformed stored hash should read as "wrong password", not a 500.
        return False


# bcrypt is deliberately slow and CPU-bound — 50-200ms of straight-line C at the
# default cost factor. Called directly from an `async def` handler it does not
# just make that one request slow: it blocks the worker's entire event loop, so
# every generation currently streaming in this process stalls for the duration
# of someone else's login. That is the exact failure this whole project exists
# to avoid, so the request path must never call the sync versions above.
#
# anyio's thread pool is the one FastAPI already runs sync dependencies on, so
# this borrows the executor that is there rather than starting another.


async def hash_password_async(plain: str) -> str:
    return await run_sync(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await run_sync(verify_password, plain, hashed)


def create_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
        # A unique id per token, so an individual token can be revoked. Without
        # it the only handle on a token is its subject, and revoking would mean
        # signing every one of that user's sessions out at once.
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_claims(token: str) -> dict | None:
    """The token's claims, or None if it is invalid or expired."""
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def decode_token(token: str) -> str | None:
    """Return the user id a token carries, or None if it is invalid or expired.

    Signature and expiry only — it does not know about revocation, because that
    needs Redis and this module is deliberately synchronous. `auth.optional_user`
    is where the two are combined, and it is the only place tokens are accepted.
    """
    payload = decode_claims(token)
    if payload is None:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None
