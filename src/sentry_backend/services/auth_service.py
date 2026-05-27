"""Authentication business logic."""
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.repository.user_repo import get_user_by_email
from sentry_backend.schemas.auth import TokenPair
from sentry_backend.security import create_user_token, verify_password


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    """Return the User on valid email+password, else None."""
    user = await get_user_by_email(db, email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def issue_tokens(user_id: UUID) -> TokenPair:
    return TokenPair(
        access_token=create_user_token(user_id, "access"),
        refresh_token=create_user_token(user_id, "refresh"),
    )
