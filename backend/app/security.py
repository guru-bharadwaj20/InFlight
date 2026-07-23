"""Password hashing and signed session tokens.

Kept separate from the auth router so the hashing and token scheme can be tested
and swapped without touching request handling. bcrypt for passwords (slow by
design, salted per hash), a short JWT for the session.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

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


def create_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> str | None:
    """Return the user id a token carries, or None if it is invalid or expired."""
    try:
        payload = jwt.decode(
            token, get_settings().jwt_secret, algorithms=[ALGORITHM]
        )
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None
