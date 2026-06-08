"""Customer-facing org self-service — team management + email invites.

Scoped to the caller's own organization (deps.tenancy). Distinct from the
super-admin/platform schemas in schemas/org.py + schemas/admin.py.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from sentry_backend.db.models.organization import OrgRole


class InviteCreate(BaseModel):
    """Org owner/admin invites an email to their org with a role."""

    email: EmailStr
    role: OrgRole = OrgRole.staff

    @field_validator("role")
    @classmethod
    def _no_owner(cls, v: OrgRole) -> OrgRole:
        if v == OrgRole.owner:
            raise ValueError("cannot invite someone as owner")
        return v


class InviteResult(BaseModel):
    """Returned after creating an invite. `invite_url` lets the admin share the
    link manually when email isn't configured (`emailed=false`)."""

    id: UUID
    email: str
    role: OrgRole
    invite_url: str
    emailed: bool
    expires_at: datetime


class PendingInvite(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: OrgRole
    expires_at: datetime
    created_at: datetime


class AcceptInvite(BaseModel):
    """Public — the invitee sets their own password to activate the account."""

    token: str = Field(min_length=10)
    password: str = Field(min_length=8, max_length=128)


class MemberUpdate(BaseModel):
    """Org admin toggles a member's access (lock = is_active False)."""

    is_active: bool
