"""Super-admin only — manage orgs, users, memberships, dashboard stats."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.alert import Alert
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.store import Store
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.repository import ai_node_repo, lead_repo, org_repo, user_repo
from sentry_backend.schemas.admin import (
    AdminStats,
    OrgMemberPublic,
    UserAdminUpdate,
    would_self_lockout,
)
from sentry_backend.schemas.ai_node import (
    AiNodePublic,
    AiNodeUpdate,
    PairingCodePublic,
)
from sentry_backend.schemas.auth import UserPublic
from sentry_backend.schemas.lead import LeadPublic, LeadUpdate
from sentry_backend.schemas.org import (
    OrganizationCreate,
    OrganizationPublic,
    UserInvite,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/stats", response_model=AdminStats)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> AdminStats:
    async def _count(model: type) -> int:
        result = await db.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())

    return AdminStats(
        orgs=await org_repo.count_orgs(db),
        users=await user_repo.count_users(db),
        stores=await _count(Store),
        cameras=await _count(Camera),
        alerts=await _count(Alert),
    )


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


@router.get("/orgs/{org_id}", response_model=OrganizationPublic)
async def get_org(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> OrganizationPublic:
    org = await org_repo.get_org(db, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return OrganizationPublic.model_validate(org)


@router.get("/orgs/{org_id}/members", response_model=list[OrgMemberPublic])
async def list_org_members(
    org_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[OrgMemberPublic]:
    if await org_repo.get_org(db, org_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    members = await org_repo.list_members(db, org_id)
    return [
        OrgMemberPublic(user=UserPublic.model_validate(user), role=role) for user, role in members
    ]


@router.get("/users", response_model=list[UserPublic])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[UserPublic]:
    users = await user_repo.list_users(db)
    return [UserPublic.model_validate(u) for u in users]


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


@router.patch("/users/{user_id}", response_model=UserPublic)
async def update_user(
    user_id: UUID,
    body: UserAdminUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_super_admin)],
) -> UserPublic:
    if would_self_lockout(actor_id=str(actor.id), target_id=str(user_id), update=body):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot revoke your own super-admin access",
        )

    user = await user_repo.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user = await user_repo.update_user_flags(
        db,
        user,
        is_active=body.is_active,
        is_super_admin=body.is_super_admin,
    )
    return UserPublic.model_validate(user)


@router.get("/leads", response_model=list[LeadPublic])
async def list_leads(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
    limit: int = 200,
    offset: int = 0,
) -> list[LeadPublic]:
    leads = await lead_repo.list_leads(db, limit=min(limit, 500), offset=max(offset, 0))
    return [LeadPublic.model_validate(lead) for lead in leads]


@router.patch("/leads/{lead_id}", response_model=LeadPublic)
async def update_lead(
    lead_id: UUID,
    body: LeadUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> LeadPublic:
    lead = await lead_repo.get_lead(db, lead_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    lead = await lead_repo.update_lead(db, lead, status=body.status, notes=body.notes)
    return LeadPublic.model_validate(lead)


# ── AI nodes (compute boxes running sentry-ai) ──────────────────────────
@router.post(
    "/ai-nodes/pairing-codes",
    response_model=PairingCodePublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_node_pairing_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_super_admin)],
) -> PairingCodePublic:
    code = await ai_node_repo.create_pairing_code(db, created_by_user_id=user.id)
    return PairingCodePublic(code=code.code, expires_at=code.expires_at)


@router.get("/ai-nodes", response_model=list[AiNodePublic])
async def list_ai_nodes(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> list[AiNodePublic]:
    nodes = await ai_node_repo.list_nodes(db)
    return [AiNodePublic.model_validate(n) for n in nodes]


@router.post("/ai-nodes/{node_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_ai_node(
    node_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> None:
    node = await ai_node_repo.get_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI node not found")
    await ai_node_repo.deactivate_node(db, node)


@router.patch("/ai-nodes/{node_id}", response_model=AiNodePublic)
async def update_ai_node(
    node_id: UUID,
    body: AiNodeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[User, Depends(require_super_admin)],
) -> AiNodePublic:
    node = await ai_node_repo.get_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI node not found")
    node = await ai_node_repo.update_node(
        db,
        node,
        name=body.name,
        enabled=body.enabled,
        provider=body.provider,
        frame_skip=body.frame_skip,
    )
    return AiNodePublic.model_validate(node)
