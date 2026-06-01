"""User CRUD operations."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.security import hash_password


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.email))
    return list(result.scalars().all())


async def count_users(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def update_user_flags(
    db: AsyncSession,
    user: User,
    *,
    is_active: bool | None = None,
    is_super_admin: bool | None = None,
) -> User:
    """Apply the provided flag changes in-place and flush. ``None`` leaves a
    field untouched."""
    if is_active is not None:
        user.is_active = is_active
    if is_super_admin is not None:
        user.is_super_admin = is_super_admin
    await db.flush()
    return user


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def create_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    is_super_admin: bool = False,
    is_active: bool = True,
) -> User:
    user = User(
        email=email,
        hashed_password=hash_password(password),
        is_super_admin=is_super_admin,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    return user
