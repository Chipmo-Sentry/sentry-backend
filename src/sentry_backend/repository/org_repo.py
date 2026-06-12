"""Organization + membership CRUD."""

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.organization import (
    Organization,
    OrganizationMember,
    OrgRole,
)
from sentry_backend.db.models.user import User


async def list_orgs(db: AsyncSession) -> list[Organization]:
    result = await db.execute(select(Organization).order_by(Organization.name))
    return list(result.scalars().all())


async def count_orgs(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(Organization))
    return int(result.scalar_one())


async def list_members(db: AsyncSession, organization_id: UUID) -> list[tuple[User, OrgRole]]:
    """Return (user, role) pairs for every member of ``organization_id``,
    ordered by email."""
    result = await db.execute(
        select(User, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(User.email)
    )
    return [(row[0], row[1]) for row in result.all()]


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


async def get_member_role(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID
) -> OrgRole | None:
    """The user's role in ``organization_id``, or None if not a member."""
    result = await db.execute(
        select(OrganizationMember.role).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def remove_membership(db: AsyncSession, *, user_id: UUID, organization_id: UUID) -> None:
    await db.execute(
        delete(OrganizationMember).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == organization_id,
        )
    )
    await db.flush()
