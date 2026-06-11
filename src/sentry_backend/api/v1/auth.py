"""Auth router — login / logout / refresh / me."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.ratelimit import limiter
from sentry_backend.repository.user_repo import get_user_by_id
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
            detail="Имэйл эсвэл нууц үг буруу байна.",
        )
    tokens = issue_tokens(user.id, user.token_version)
    set_auth_cookies(response, tokens.access_token, tokens.refresh_token)
    return LoginResponse(user=UserPublic.model_validate(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    sentry_access: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Clear this device's cookies AND server-side revoke every outstanding
    token for the user by bumping ``token_version``. Best-effort on the bump:
    a missing/expired/invalid token still clears cookies (idempotent logout),
    it just can't invalidate other devices."""
    token = sentry_access or _bearer_token(authorization)
    if token:
        try:
            payload = decode_user_token(token)
            user_id = UUID(payload["sub"])
        except (ValueError, KeyError):
            pass
        else:
            user = await get_user_by_id(db, user_id)
            if user is not None:
                user.token_version += 1
                await db.commit()
    clear_auth_cookies(response)


def _bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(maxsplit=1)[1]
    return None


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
async def refresh(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    sentry_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    expired = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Нэвтрэх хугацаа дууссан байна. Дахин нэвтэрнэ үү.",
    )
    if not sentry_refresh:
        raise expired
    try:
        payload = decode_user_token(sentry_refresh)
    except ValueError as e:
        raise expired from e
    if payload.get("typ") != "refresh":
        raise expired
    try:
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise expired from e

    # Honor revocation: a refresh token whose tv no longer matches the user's
    # token_version (logout / password change) must not mint fresh tokens.
    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active or payload.get("tv", 0) != user.token_version:
        raise expired

    tokens = issue_tokens(user.id, user.token_version)
    set_auth_cookies(response, tokens.access_token, tokens.refresh_token)
    # Cookies carry the new tokens; nothing in the body (ADR-0017).


@router.get("/me", response_model=UserPublic)
async def me(
    user: Annotated[User, Depends(get_current_user)],
) -> UserPublic:
    return UserPublic.model_validate(user)
