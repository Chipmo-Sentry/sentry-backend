"""Resolve the current Organization for tenant-scoped endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.organization import OrganizationMember, OrgRole
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db

# Roles allowed to mutate org resources (stores, cameras, clip upload).
_ADMIN_ROLES = (OrgRole.owner, OrgRole.admin)


async def get_current_organization_id(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
) -> UUID:
    """Determine the org the request is acting on.

    Resolution order:
    1. super_admin + X-Org-Id header → use header value (any org).
    2. User has exactly 1 membership → use that.
    3. User has multiple memberships + X-Org-Id → use header if user is a member.
    4. Multiple memberships without header → 400.
    5. No memberships at all → 403.
    """
    requested_org: UUID | None = None
    if x_org_id:
        try:
            requested_org = UUID(x_org_id)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid X-Org-Id header (must be UUID)",
            ) from e

    if user.is_super_admin and requested_org is not None:
        return requested_org

    result = await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)
    )
    memberships: list[UUID] = list(result.scalars().all())

    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of any organization",
        )

    if requested_org is not None:
        if requested_org not in memberships:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of requested organization",
            )
        return requested_org

    if len(memberships) == 1:
        return memberships[0]

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Multiple memberships — set X-Org-Id header",
    )


async def get_current_organization_id_admin(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
) -> UUID:
    """Like get_current_organization_id but requires owner/admin role.

    Gate for MUTATING org resources (ADR-0009): staff get read-only access, so
    they must not create/delete stores/cameras or upload clips. Super-admins
    bypass the role check.
    """
    org_id = await get_current_organization_id(user, db, x_org_id)
    if user.is_super_admin:
        return org_id
    result = await db.execute(
        select(OrganizationMember.role).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.organization_id == org_id,
        )
    )
    role = result.scalar_one_or_none()
    if role not in _ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires owner or admin role",
        )
    return org_id
