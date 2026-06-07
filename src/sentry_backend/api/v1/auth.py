"""Auth router — login / logout / refresh / me."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.ratelimit import limiter
from sentry_backend.schemas.auth import (
    LoginRequest,
    LoginResponse,
    UserPublic,
)
from sentry_backend.security import (
    clear_auth_cookies,
    decode_user_token,
    set_auth_cookies,
)
from sentry_backend.services.auth_service import authenticate, issue_tokens
from sentry_backend.settings import get_settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
@limiter.limit(lambda: get_settings().login_rate_limit)
async def login(
    request: Request,  # noqa: ARG001 — required by slowapi's limiter
    body: LoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LoginResponse:
    user = await authenticate(db, body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    tokens = issue_tokens(user.id)
    set_auth_cookies(response, tokens.access_token, tokens.refresh_token)
    return LoginResponse(user=UserPublic.model_validate(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    clear_auth_cookies(response)


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
async def refresh(
    response: Response,
    sentry_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    if not sentry_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token",
        )
    try:
        payload = decode_user_token(sentry_refresh)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        ) from e
    if payload.get("typ") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token type",
        )
    try:
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
        ) from e

    tokens = issue_tokens(user_id)
    set_auth_cookies(response, tokens.access_token, tokens.refresh_token)
    # Cookies carry the new tokens; nothing in the body (ADR-0017).


@router.get("/me", response_model=UserPublic)
async def me(
    user: Annotated[User, Depends(get_current_user)],
) -> UserPublic:
    return UserPublic.model_validate(user)
