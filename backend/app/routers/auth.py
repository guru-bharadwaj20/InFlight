"""Signup, login, and "who am I".

Deliberately small: create a user with a hashed password, hand back a signed
token, and let the token identify the user on every later request.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import User
from ..schemas import LoginIn, SignupIn, TokenOut, UserOut
from ..security import create_token, hash_password_async, verify_password_async

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
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
    payload: LoginIn,
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
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
