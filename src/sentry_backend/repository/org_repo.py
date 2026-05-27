"""Organization + membership CRUD."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.organization import (
    Organization,
    OrganizationMember,
    OrgRole,
)


async def list_orgs(db: AsyncSession) -> list[Organization]:
    result = await db.execute(select(Organization).order_by(Organization.name))
    return list(result.scalars().all())


async def get_org(db: AsyncSession, org_id: UUID) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    return result.scalar_one_or_none()


async def get_org_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def create_org(db: AsyncSession, *, name: str, slug: str) -> Organization:
    org = Organization(name=name, slug=slug)
    db.add(org)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise
    return org


async def add_membership(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    role: OrgRole,
) -> OrganizationMember:
    member = OrganizationMember(user_id=user_id, organization_id=organization_id, role=role)
    db.add(member)
    await db.flush()
    return member
