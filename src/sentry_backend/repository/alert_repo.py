"""Alert CRUD."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.alert import Alert, AlertCategory, AlertLevel, AlertTrigger
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.clip import Clip
from sentry_backend.db.models.organization import Organization
from sentry_backend.db.models.store import Store

# Severity order (low → high) for debounce comparisons.
_LEVEL_ORDER = [AlertLevel.ignore, AlertLevel.log, AlertLevel.notify, AlertLevel.review]


async def recent_live_alert_exists(
    db: AsyncSession,
    *,
    camera_id: UUID,
    person_id: int,
    within_sec: float,
    at_or_above: AlertLevel,
) -> bool:
    """True if a live-threshold alert at level >= `at_or_above` for this (camera,
    tracked person) landed within `within_sec`. With the VLM-primary scan firing
    every few seconds, this debounces repeats of the SAME-or-lower severity (so
    the menu isn't flooded with "browsing"), while still letting an ESCALATION
    through — e.g. a theft (review) right after a browsing (log) is NOT suppressed
    because no recent review-level alert exists."""
    cutoff = datetime.now(UTC) - timedelta(seconds=within_sec)
    wanted = _LEVEL_ORDER[_LEVEL_ORDER.index(at_or_above) :]
    stmt = (
        select(Alert.id)
        .where(
            Alert.camera_id == camera_id,
            Alert.person_id == person_id,
            Alert.triggered_by == AlertTrigger.live_threshold,
            Alert.alert_level.in_(wanted),
            Alert.created_at >= cutoff,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None


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


async def list_recent_admin(
    db: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[tuple[Alert, str, str | None, str | None, list[dict[str, Any]] | None, float | None]]:
    """Recent alerts across ALL orgs (super-admin pipeline view), each joined to
    its org / store / camera display names + the source clip's EDGE score
    breakdown. Returns ``(alert, org_name, store_name, camera_name,
    edge_behavior_detail, edge_risk_pct)`` rows, newest first."""
    stmt = (
        select(
            Alert,
            Organization.name,
            Store.name,
            Camera.name,
            Clip.edge_behavior_detail,
            Clip.edge_risk_pct,
        )
        .join(Organization, Alert.organization_id == Organization.id)
        .outerjoin(Store, Alert.store_id == Store.id)
        .outerjoin(Camera, Alert.camera_id == Camera.id)
        .outerjoin(Clip, Alert.clip_id == Clip.id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = await db.execute(stmt)
    return [(a, o, s, c, ed, er) for a, o, s, c, ed, er in rows.all()]


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
    actions: list[str] | None = None,
    triggered_by: AlertTrigger = AlertTrigger.manual_upload,
    person_id: int | None = None,
    peak_risk_pct: float | None = None,
    triggered_behaviors: list[str] | None = None,
    triggered_sequences: list[str] | None = None,
    triggered_behavior_detail: list[dict[str, Any]] | None = None,
    embedding: list[float] | None = None,
) -> Alert:
    alert = Alert(
        clip_id=clip_id,
        organization_id=organization_id,
        store_id=store_id,
        camera_id=camera_id,
        category=category,
        actions=actions,
        confidence=confidence,
        reasoning=reasoning,
        model_name=model_name,
        alert_level=alert_level,
        inference_latency_ms=inference_latency_ms,
        triggered_by=triggered_by,
        person_id=person_id,
        peak_risk_pct=peak_risk_pct,
        triggered_behaviors=triggered_behaviors,
        triggered_sequences=triggered_sequences,
        triggered_behavior_detail=triggered_behavior_detail,
        embedding=embedding,
    )
    db.add(alert)
    await db.flush()
    return alert
