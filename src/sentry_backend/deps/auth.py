"""Current-user + role-based dependencies."""

from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.deps.db import get_db
from sentry_backend.repository.user_repo import get_user_by_id
from sentry_backend.security import decode_user_token

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Нэвтрэх шаардлагатай. Дахин нэвтэрнэ үү.",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_token(
    cookie_token: str | None,
    auth_header: str | None,
) -> str:
    if cookie_token:
        return cookie_token
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(maxsplit=1)[1]
    raise CREDENTIALS_EXCEPTION


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    sentry_access: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = _extract_token(sentry_access, authorization)
    try:
        payload = decode_user_token(token)
    except ValueError as e:
        raise CREDENTIALS_EXCEPTION from e

    if payload.get("typ") != "access":
        raise CREDENTIALS_EXCEPTION

    try:
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise CREDENTIALS_EXCEPTION from e

    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_EXCEPTION
    return user


async def require_super_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not user.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Зөвхөн super-admin эрхтэй.",
        )
    return user
