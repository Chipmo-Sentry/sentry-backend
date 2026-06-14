"""Feedback CRUD."""

from collections.abc import Sequence
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


async def get_latest_for_alert(db: AsyncSession, alert_id: UUID) -> Feedback | None:
    """Latest verdict on an alert from ANY user (the alert's current decision)."""
    result = await db.execute(
        select(Feedback)
        .where(Feedback.alert_id == alert_id)
        .order_by(Feedback.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_verdicts_for_alerts(
    db: AsyncSession, alert_ids: Sequence[UUID]
) -> dict[UUID, FeedbackVerdict]:
    """Map each alert_id → its latest verdict (newest first wins), for the list view.

    One query instead of N: we read every feedback row for these alerts ordered
    newest-first and keep the first (latest) seen per alert.
    """
    if not alert_ids:
        return {}
    result = await db.execute(
        select(Feedback.alert_id, Feedback.verdict)
        .where(Feedback.alert_id.in_(alert_ids))
        .order_by(Feedback.created_at.desc())
    )
    out: dict[UUID, FeedbackVerdict] = {}
    for alert_id, verdict in result.all():
        out.setdefault(alert_id, verdict)  # first seen = latest (desc order)
    return out


async def upsert_feedback(
    db: AsyncSession,
    *,
    alert_id: UUID,
    user_id: UUID,
    verdict: FeedbackVerdict,
    notes: str | None,
) -> tuple[Feedback, bool]:
    """Set THIS user's verdict on an alert — one row per (alert, user).

    Returns ``(feedback, changed)`` where ``changed`` is True when the verdict is
    new or different from what the user had (so the caller only writes a RAG case
    on a real decision, not on an idempotent re-submit). This is what makes the UI
    "one answer per alert" instead of piling up duplicate rows + RAG cases.
    """
    existing = await get_feedback_for_alert(db, alert_id, user_id)
    if existing is not None:
        changed = existing.verdict != verdict or existing.notes != notes
        existing.verdict = verdict
        existing.notes = notes
        await db.flush()
        return existing, changed
    fb = Feedback(alert_id=alert_id, user_id=user_id, verdict=verdict, notes=notes)
    db.add(fb)
    await db.flush()
    return fb, True
