"""Super-admin dashboard + user management schemas."""

from pydantic import BaseModel, ConfigDict

from sentry_backend.db.models.organization import OrgRole
from sentry_backend.schemas.auth import UserPublic


class AdminStats(BaseModel):
    """Top-level counts for the super-admin dashboard."""

    orgs: int
    users: int
    stores: int
    cameras: int
    alerts: int
    # Usage detail (docs/19 Phase 2)
    cameras_enabled: int = 0
    ai_nodes: int = 0
    ai_nodes_online: int = 0
    alerts_24h: int = 0


class OrgMemberPublic(BaseModel):
    """A user together with their role inside one organization."""

    model_config = ConfigDict(from_attributes=True)

    user: UserPublic
    role: OrgRole


class UserAdminUpdate(BaseModel):
    """Partial update of a user's super-admin / active flags.

    Both fields optional; only provided keys are applied.
    """

    is_active: bool | None = None
    is_super_admin: bool | None = None


def would_self_lockout(
    *,
    actor_id: str,
    target_id: str,
    update: UserAdminUpdate,
) -> bool:
    """True if ``actor`` is editing *their own* account in a way that would
    revoke their own super-admin access (demote self or deactivate self).

    Pure function so it can be unit-tested without a DB. The API layer calls
    this before applying an update and rejects with 400 when it returns True.
    """
    if actor_id != target_id:
        return False
    if update.is_super_admin is False:
        return True
    return update.is_active is False
