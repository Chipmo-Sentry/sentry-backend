"""Feedback CRUD."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.feedback import Feedback, FeedbackVerdict


async def get_feedback_for_alert(
    db: AsyncSession, alert_id: UUID, user_id: UUID
) -> Feedback | None:
    """One user × one alert — return latest if exists."""
    result = await db.execute(
        select(Feedback)
        .where(Feedback.alert_id == alert_id, Feedback.user_id == user_id)
        .order_by(Feedback.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_feedback(
    db: AsyncSession,
    *,
    alert_id: UUID,
    user_id: UUID,
    verdict: FeedbackVerdict,
    notes: str | None,
) -> Feedback:
    fb = Feedback(
        alert_id=alert_id,
        user_id=user_id,
        verdict=verdict,
        notes=notes,
    )
    db.add(fb)
    await db.flush()
    return fb
