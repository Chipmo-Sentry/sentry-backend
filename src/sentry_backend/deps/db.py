"""Database session dependency for FastAPI."""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.session import get_sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
