"""Alert CRUD."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.alert import Alert, AlertCategory, AlertLevel


async def get_alert_for_org(db: AsyncSession, alert_id: UUID, org_id: UUID) -> Alert | None:
    result = await db.execute(
        select(Alert).where(Alert.id == alert_id, Alert.organization_id == org_id)
    )
    return result.scalar_one_or_none()


async def list_alerts_for_org(
    db: AsyncSession,
    org_id: UUID,
    *,
    store_id: UUID | None = None,
    camera_id: UUID | None = None,
    min_level: AlertLevel | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Alert]:
    stmt = select(Alert).where(Alert.organization_id == org_id)
    if store_id is not None:
        stmt = stmt.where(Alert.store_id == store_id)
    if camera_id is not None:
        stmt = stmt.where(Alert.camera_id == camera_id)
    if min_level is not None:
        # Only "notify" and "review" if min_level=notify
        levels_in_order = [
            AlertLevel.ignore,
            AlertLevel.log,
            AlertLevel.notify,
            AlertLevel.review,
        ]
        wanted = levels_in_order[levels_in_order.index(min_level) :]
        stmt = stmt.where(Alert.alert_level.in_(wanted))
    stmt = stmt.order_by(Alert.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_alert(
    db: AsyncSession,
    *,
    clip_id: UUID,
    organization_id: UUID,
    store_id: UUID | None,
    camera_id: UUID | None,
    category: AlertCategory,
    confidence: float,
    reasoning: str,
    model_name: str,
    alert_level: AlertLevel,
    inference_latency_ms: int,
) -> Alert:
    alert = Alert(
        clip_id=clip_id,
        organization_id=organization_id,
        store_id=store_id,
        camera_id=camera_id,
        category=category,
        confidence=confidence,
        reasoning=reasoning,
        model_name=model_name,
        alert_level=alert_level,
        inference_latency_ms=inference_latency_ms,
    )
    db.add(alert)
    await db.flush()
    return alert
