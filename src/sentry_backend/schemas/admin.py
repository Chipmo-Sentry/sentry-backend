"""Super-admin dashboard + user management schemas."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from sentry_backend.db.models.organization import OrgRole
from sentry_backend.schemas.alert import AlertPublic
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


class StoreAdminRow(BaseModel):
    """One store with its org name + camera count — drives the superadmin store
    pickers (e.g. the per-store edge-config editor). Cross-org: super-admin only."""

    id: str
    name: str
    organization_id: str
    organization_name: str
    camera_count: int = 0
    # Per-store cloud push target (None → global AGENT_STREAM_PUSH_URL env). Shown
    # and editable in superadmin so a vast.ai restart can be repointed live.
    agent_stream_push_url: str | None = None


class StoreAdminUpdate(BaseModel):
    """Superadmin partial store edit. Only the per-store push target for now;
    empty string clears it (back to the global env URL)."""

    agent_stream_push_url: str | None = Field(default=None, max_length=500)


class AdminAlertRow(AlertPublic):
    """One alert enriched with org/store/camera display names for the superadmin
    pipeline ("Урсгал") page. Each row is one problematic clip's full journey:
    camera → behaviours (triggered_*) → VLM (reasoning/confidence/model) →
    decision (alert_level/category) → review (feedback_verdict)."""

    organization_name: str
    store_name: str | None = None
    camera_name: str | None = None

    @field_serializer("created_at")
    def _ser_created(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.isoformat()


class TelegramConfigView(BaseModel):
    """Superadmin read view of the platform Telegram bot config. The full token
    is NEVER returned — only whether one is set + a last-4 hint."""

    configured: bool
    token_hint: str | None = None


class TelegramConfigUpdate(BaseModel):
    """Set/clear the platform Telegram bot token. A non-empty ``bot_token`` sets
    it; an empty string clears the integration. ``None`` is rejected (the field
    must be present) so a save always has explicit intent."""

    bot_token: str = Field(max_length=200)
