"""Super-admin only — create orgs, invite users."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.repository import org_repo, user_repo
from sentry_backend.schemas.auth import UserPublic
from sentry_backend.schemas.org import (
    OrganizationCreate,
    OrganizationPublic,
    UserInvite,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/orgs", response_model=list[OrganizationPublic])
async def list_orgs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[OrganizationPublic]:
    orgs = await org_repo.list_orgs(db)
    return [OrganizationPublic.model_validate(o) for o in orgs]


@router.post(
    "/orgs",
    response_model=OrganizationPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_org(
    body: OrganizationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> OrganizationPublic:
    if await org_repo.get_org_by_slug(db, body.slug) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Slug '{body.slug}' is already taken",
        )
    org = await org_repo.create_org(db, name=body.name, slug=body.slug)
    return OrganizationPublic.model_validate(org)


@router.post(
    "/users",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
)
async def invite_user(
    body: UserInvite,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> UserPublic:
    # Reject duplicate email
    if await user_repo.get_user_by_email(db, body.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Verify org exists
    org = await org_repo.get_org(db, body.organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    user = await user_repo.create_user(
        db,
        email=body.email,
        password=body.password,
        is_super_admin=body.is_super_admin,
    )

    try:
        await org_repo.add_membership(
            db,
            user_id=user.id,
            organization_id=body.organization_id,
            role=body.role,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already a member of this organization",
        ) from None

    return UserPublic.model_validate(user)
