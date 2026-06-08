"""Invitation CRUD."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.invitation import Invitation
from sentry_backend.db.models.organization import OrgRole


def _now() -> datetime:
    return datetime.now(UTC)


async def create_invitation(
    db: AsyncSession,
    *,
    email: str,
    organization_id: UUID,
    role: OrgRole,
    token_hash: str,
    expires_at: datetime,
    invited_by: UUID | None,
) -> Invitation:
    inv = Invitation(
        email=email,
        organization_id=organization_id,
        role=role,
        token_hash=token_hash,
        expires_at=expires_at,
        invited_by_user_id=invited_by,
    )
    db.add(inv)
    await db.flush()
    return inv


async def get_valid_by_token_hash(db: AsyncSession, token_hash: str) -> Invitation | None:
    """A not-yet-accepted, not-expired invitation for this token hash."""
    res = await db.execute(
        select(Invitation).where(
            Invitation.token_hash == token_hash,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > _now(),
        )
    )
    return res.scalar_one_or_none()


async def list_pending_for_org(db: AsyncSession, organization_id: UUID) -> list[Invitation]:
    res = await db.execute(
        select(Invitation)
        .where(Invitation.organization_id == organization_id, Invitation.accepted_at.is_(None))
        .order_by(Invitation.created_at.desc())
    )
    return list(res.scalars().all())


async def mark_accepted(db: AsyncSession, inv: Invitation) -> None:
    inv.accepted_at = _now()
    await db.flush()


async def get_invitation(db: AsyncSession, invitation_id: UUID) -> Invitation | None:
    res = await db.execute(select(Invitation).where(Invitation.id == invitation_id))
    return res.scalar_one_or_none()


async def delete_invitation(db: AsyncSession, inv: Invitation) -> None:
    await db.delete(inv)
    await db.flush()
