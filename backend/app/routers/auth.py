"""Signup, login, and "who am I".

Deliberately small: create a user with a hashed password, hand back a signed
token, and let the token identify the user on every later request.
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ratelimit, redis_client
from ..auth import current_user
from ..config import Settings, get_settings
from ..db import get_session
from ..models import User
from ..schemas import LoginIn, SignupIn, TokenOut, UserOut, WsTicketOut
from ..security import create_token, hash_password_async, verify_password_async

router = APIRouter(prefix="/auth", tags=["auth"])


def _signup_limit():
    s = get_settings()
    return ratelimit.limit_by_client(
        "signup", s.signup_rate_limit_per_ip, s.signup_rate_window_seconds
    )


@router.post(
    "/signup",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_signup_limit())],
)
async def signup(
    payload: SignupIn,
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    email = payload.email.lower()

    # Check first for a friendly error, but the unique constraint is the real
    # guard: two signups racing the same email both pass the check, and only the
    # constraint stops the second from committing.
    existing = await session.execute(select(User.id).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")

    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=await hash_password_async(payload.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")
    await session.refresh(user)

    return TokenOut(token=create_token(user.id), user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenOut)
async def login(
    request: Request,
    payload: LoginIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    # Before the lookup and before bcrypt, so a throttled guess costs neither a
    # query nor a hash.
    await ratelimit.limit_login(request, payload.email, settings)

    result = await session.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()

    # One message for both "no such email" and "wrong password", so the response
    # does not reveal which emails have accounts.
    if user is None or not await verify_password_async(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect email or password")

    return TokenOut(token=create_token(user.id), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> User:
    return user


@router.post("/ws-ticket", response_model=WsTicketOut)
async def ws_ticket(user: User = Depends(current_user)) -> WsTicketOut:
    """Mint a short-lived, single-use ticket for opening a WebSocket.

    A browser cannot put an Authorization header on a WebSocket, so something has
    to go in the URL. It should not be the session token: that is a week-long
    credential, and URLs end up in proxy logs, access logs, browser history,
    Referer headers and error trackers, none of which are places to keep one.

    This endpoint is reached with the token in a header, where it belongs, and
    hands back an opaque value that is good for one connection and half a minute.
    By the time anything that logged it is read, it has already expired.
    """
    ticket = secrets.token_urlsafe(32)
    await redis_client.create_ws_ticket(ticket, user.id)
    return WsTicketOut(
        ticket=ticket, expires_in=redis_client.WS_TICKET_TTL_SECONDS
    )
