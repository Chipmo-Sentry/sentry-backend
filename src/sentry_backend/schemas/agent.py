"""Agent pairing + agent-facing schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sentry_backend.db.models.camera import ComputeTier
from sentry_backend.schemas.camera import MEDIAMTX_PATH_PATTERN


class PairingCodePublic(BaseModel):
    """Returned to the admin UI after generating a code."""

    code: str
    store_id: UUID
    expires_at: datetime


class AgentPublic(BaseModel):
    """A paired agent, shown in the admin UI."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str | None
    store_id: UUID
    is_active: bool
    last_seen_at: datetime | None
    created_at: datetime


class AgentPairRequest(BaseModel):
    code: str = Field(min_length=4, max_length=8)
    name: str | None = Field(default=None, max_length=255)


class AgentPairResult(BaseModel):
    """Returned to the agent on successful pairing."""

    agent_token: str
    agent_id: UUID
    organization_id: UUID
    store_id: UUID
    store_name: str


class AgentCameraCreate(BaseModel):
    """Camera registration payload from a paired agent (store comes from the
    agent's token, not the body)."""

    name: str = Field(min_length=1, max_length=255)
    rtsp_url: str = Field(min_length=1)
    # Agent-supplied, so validate it the same way as the user-facing schema —
    # it reaches the recordings filesystem path and the MediaMTX control API.
    mediamtx_path: str | None = Field(default=None, pattern=MEDIAMTX_PATH_PATTERN)
    # Default 11 = yellow band (matches the Camera model). The agent already sends
    # 11; this fallback ensures an older agent that omits it isn't stuck at deep-red.
    risk_threshold: float = Field(default=11.0, ge=0.0, le=100.0)
    # ADR-0029 — compute tier; default cloud. An older agent that omits it stays cloud.
    compute_tier: ComputeTier = ComputeTier.cloud


class AgentCameraUpdate(BaseModel):
    """Partial camera update from a paired agent (edit connection / settings).

    All fields optional — only the provided ones change. mediamtx_path is NOT
    editable here: it is the stream identity the live pipeline keys on, so the
    agent edits the connection (rtsp_url) and name, never the path."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, min_length=1)
    risk_threshold: float | None = Field(default=None, ge=0.0, le=100.0)
    compute_tier: ComputeTier | None = None


class AgentStreamConfig(BaseModel):
    """Where the agent should publish its camera streams (cloud topology).

    push_enabled=False means MediaMTX pulls cameras directly (local/on-LAN);
    the agent does NOT push and runs no ffmpeg relays.
    """

    push_enabled: bool
    push_rtsp_base: str | None = None  # e.g. "rtsp://media.sentry.chipmo.mn:8554"
    publish_user: str | None = None
    publish_pass: str | None = None
