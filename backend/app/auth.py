"""Who is making the request.

`current_user` is the dependency every protected route leans on; `optional_user`
is for the few places (the WebSocket, health) where being signed out is not an
error. Both read a bearer token, never a cookie — the SPA talks to this API
cross-origin, and a bearer header sidesteps the SameSite/Secure friction that
cross-site cookies bring.
"""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import User
from .security import decode_token


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def optional_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    token = _bearer(authorization)
    if not token:
        return None
    user_id = decode_token(token)
    if not user_id:
        return None
    return await session.get(User, user_id)


async def current_user(
    user: User | None = Depends(optional_user),
) -> User:
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
