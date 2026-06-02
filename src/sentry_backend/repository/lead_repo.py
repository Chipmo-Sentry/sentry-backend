"""Lead CRUD — public capture + admin triage."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.lead import Lead, LeadStatus


async def create_lead(
    db: AsyncSession,
    *,
    email: str,
    name: str | None,
    organization: str | None,
    phone: str | None,
    industry: str | None,
    camera_count: int | None,
    source: str,
) -> Lead:
    lead = Lead(
        email=email,
        name=name,
        organization=organization,
        phone=phone,
        industry=industry,
        camera_count=camera_count,
        source=source,
    )
    db.add(lead)
    await db.flush()
    return lead


async def list_leads(db: AsyncSession, *, limit: int = 200, offset: int = 0) -> list[Lead]:
    result = await db.execute(
        select(Lead).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def count_leads(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(Lead))
    return int(result.scalar_one())


async def get_lead(db: AsyncSession, lead_id: UUID) -> Lead | None:
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    return result.scalar_one_or_none()


async def update_lead(
    db: AsyncSession,
    lead: Lead,
    *,
    status: LeadStatus | None,
    notes: str | None,
) -> Lead:
    if status is not None:
        lead.status = status
    if notes is not None:
        lead.notes = notes
    await db.flush()
    return lead
