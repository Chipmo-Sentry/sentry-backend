"""Org self-service: team members + email invites (owner/admin scope).

Distinct from /api/v1/admin/* (super-admin, platform-wide). Every route here is
scoped to the CALLER's organization via deps.tenancy, so a customer manages only
their own org's users — they can never create organizations or touch other
tenants. Org creation stays super-admin-only (platform onboarding).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.db.models.organization import OrgRole
from sentry_backend.db.models.user import User
from sentry_backend.deps.auth import get_current_user
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import invitation_repo, org_repo, user_repo
from sentry_backend.schemas.admin import OrgMemberPublic
from sentry_backend.schemas.auth import UserPublic
from sentry_backend.schemas.org_team import (
    AcceptInvite,
    InviteCreate,
    InviteResult,
    MemberUpdate,
    OrgDeleteConfirm,
    PendingInvite,
)
from sentry_backend.services import email_service, event_log, org_delete
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.api.org")
router = APIRouter(prefix="/api/v1/org", tags=["org"])


@router.get("/members", response_model=list[OrgMemberPublic])
async def list_members(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> list[OrgMemberPublic]:
    """Members of the caller's org (any member may view their own team)."""
    rows = await org_repo.list_members(db, org_id)
    return [OrgMemberPublic(user=UserPublic.model_validate(u), role=r) for u, r in rows]


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_my_org(
    body: OrgDeleteConfirm,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Owner deletes their own organization (T15 #3 — privacy policy promise).

    Owner-only (admins manage the team; only the owner can erase the tenant).
    The caller must retype the org slug (`confirm_slug`) so a stray DELETE can't
    wipe everything. All org data cascades in the DB; clip evidence files are
    removed from disk first. Members keep their user accounts but lose all
    org-scoped access the moment the membership rows cascade away."""
    role = await org_repo.get_member_role(db, user_id=actor.id, organization_id=org_id)
    if role != OrgRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Зөвхөн байгууллагын эзэн (owner) байгууллагаа устгах боломжтой.",
        )
    org = await org_repo.get_org(db, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Байгууллага олдсонгүй.",
        )
    if body.confirm_slug != org.slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Баталгаажуулахын тулд байгууллагын slug-ээ зөв оруулна уу.",
        )
    org_name, org_slug = org.name, org.slug
    sweep = await org_delete.delete_organization(db, org)
    log.info(
        "org.self_deleted",
        org_id=str(org_id),
        slug=org_slug,
        by=str(actor.id),
        clips_removed=sweep.removed,
    )
    # Platform-level (organization_id=None): an org-scoped row would cascade away
    # with the org we just deleted, so the audit trail must outlive the tenant.
    await event_log.emit(
        db,
        event_type=EventType.org_deleted,
        severity=EventSeverity.warning,
        message=f"Байгууллага устлаа: {org_name} ({org_slug})",
        actor_user_id=actor.id,
        actor_label=actor.email,
        detail={"slug": org_slug, "clips_removed": sweep.removed},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/invitations", response_model=list[PendingInvite])
async def list_invitations(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> list[PendingInvite]:
    rows = await invitation_repo.list_pending_for_org(db, org_id)
    return [PendingInvite.model_validate(i) for i in rows]


@router.post("/invitations", response_model=InviteResult, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InviteCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
    actor: Annotated[User, Depends(get_current_user)],
) -> InviteResult:
    """Invite an email to the caller's org. Emails a tokenized accept link;
    if SMTP isn't configured the link is returned for manual sharing."""
    if await user_repo.get_user_by_email(db, body.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ имэйл аль хэдийн бүртгэлтэй байна.",
        )
    s = get_settings()
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(hours=s.invite_ttl_hours)
    inv = await invitation_repo.create_invitation(
        db,
        email=body.email,
        organization_id=org_id,
        role=body.role,
        token_hash=token_hash,
        expires_at=expires_at,
        invited_by=actor.id,
    )
    invite_url = f"{s.app_base_url.rstrip('/')}/accept-invite?token={raw}"

    org = await org_repo.get_org(db, org_id)
    org_name = org.name if org else "Sentry"
    days = max(1, s.invite_ttl_hours // 24)
    emailed = await email_service.send_email(
        to=body.email,
        subject=f"{org_name} — Sentry урилга",
        body_text=(
            f"Сайн байна уу,\n\nТанд {org_name} байгууллагад нэгдэх урилга ирлээ. "
            f"Доорх холбоосоор орж нууц үгээ тохируулна уу:\n\n{invite_url}\n\n"
            f"Холбоос {days} хоногийн дараа хүчингүй болно.\n\n— Sentry"
        ),
        body_html=(
            f"<p>Сайн байна уу,</p><p>Танд <b>{org_name}</b> байгууллагад нэгдэх "
            f'урилга ирлээ.</p><p><a href="{invite_url}">Урилгыг хүлээн авах</a></p>'
            f"<p>Холбоос {days} хоногийн дараа хүчингүй болно.</p><p>— Sentry</p>"
        ),
    )
    log.info("org.invite_created", org_id=str(org_id), email=body.email, emailed=emailed)
    await event_log.emit(
        db,
        event_type=EventType.user_invited,
        severity=EventSeverity.info,
        message=f"Хэрэглэгч уригдлаа: {body.email} ({body.role.value})",
        organization_id=org_id,
        actor_user_id=actor.id,
        actor_label=actor.email,
        detail={"email": body.email, "role": body.role.value, "emailed": emailed},
    )
    return InviteResult(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        invite_url=invite_url,
        emailed=emailed,
        expires_at=expires_at,
    )


@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def cancel_invitation(
    invitation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> Response:
    """Revoke a pending invite. The token immediately stops working."""
    inv = await invitation_repo.get_invitation(db, invitation_id)
    if inv is None or inv.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Урилга олдсонгүй.",
        )
    await invitation_repo.delete_invitation(db, inv)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def remove_member(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
    actor: Annotated[User, Depends(get_current_user)],
) -> Response:
    if user_id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Өөрийгөө хасч болохгүй.",
        )
    rows = await org_repo.list_members(db, org_id)
    target = next(((u, r) for u, r in rows if u.id == user_id), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Энэ хэрэглэгч тус байгууллагын гишүүн биш байна.",
        )
    if target[1] == OrgRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Байгууллагын эзнийг (owner) хасч болохгүй.",
        )
    await org_repo.remove_membership(db, user_id=user_id, organization_id=org_id)
    await event_log.emit(
        db,
        event_type=EventType.member_access_changed,
        severity=EventSeverity.warning,
        message=f"Гишүүн хасагдлаа: {target[0].email}",
        organization_id=org_id,
        actor_user_id=actor.id,
        actor_label=actor.email,
        detail={"removed_user_id": str(user_id), "email": target[0].email},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/members/{user_id}", response_model=OrgMemberPublic)
async def update_member(
    user_id: UUID,
    body: MemberUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
    actor: Annotated[User, Depends(get_current_user)],
) -> OrgMemberPublic:
    """Lock (is_active=false) or unlock a member. Locking blocks their login
    immediately without deleting the account. Can't lock yourself or the owner."""
    if user_id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Өөрийгөө түгжиж болохгүй.",
        )
    rows = await org_repo.list_members(db, org_id)
    target = next(((u, r) for u, r in rows if u.id == user_id), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Энэ хэрэглэгч тус байгууллагын гишүүн биш байна.",
        )
    if target[1] == OrgRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Байгууллагын эзнийг (owner) түгжиж болохгүй.",
        )
    user = await user_repo.update_user_flags(db, target[0], is_active=body.is_active)
    log.info("org.member_access", org_id=str(org_id), user_id=str(user_id), active=body.is_active)
    await event_log.emit(
        db,
        event_type=EventType.member_access_changed,
        severity=EventSeverity.info,
        message=(
            f"Гишүүн идэвхжлээ: {user.email}"
            if body.is_active
            else f"Гишүүн түгжигдлээ: {user.email}"
        ),
        organization_id=org_id,
        actor_user_id=actor.id,
        actor_label=actor.email,
        detail={"user_id": str(user_id), "is_active": body.is_active},
    )
    return OrgMemberPublic(user=UserPublic.model_validate(user), role=target[1])


@router.post("/accept-invite", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def accept_invite(
    body: AcceptInvite,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserPublic:
    """Public — the invitee sets their password to create the account + join."""
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    inv = await invitation_repo.get_valid_by_token_hash(db, token_hash)
    if inv is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Урилга хүчингүй эсвэл хугацаа дууссан байна.",
        )
    if await user_repo.get_user_by_email(db, inv.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ имэйл аль хэдийн бүртгэлтэй байна.",
        )
    user = await user_repo.create_user(
        db, email=inv.email, password=body.password, is_super_admin=False
    )
    await org_repo.add_membership(
        db, user_id=user.id, organization_id=inv.organization_id, role=inv.role
    )
    await invitation_repo.mark_accepted(db, inv)
    log.info("org.invite_accepted", org_id=str(inv.organization_id), email=inv.email)
    await event_log.emit(
        db,
        event_type=EventType.invite_accepted,
        severity=EventSeverity.success,
        message=f"Урилга зөвшөөрөгдлөө: {inv.email}",
        organization_id=inv.organization_id,
        actor_user_id=user.id,
        actor_label=inv.email,
        detail={"role": inv.role.value if hasattr(inv.role, "value") else str(inv.role)},
    )
    return UserPublic.model_validate(user)
