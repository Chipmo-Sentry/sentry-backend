"""Edge Stage-1 contract schemas (ADR-0029).

`EdgeConfigPayload` is the tunables the store agent's config-poller fetches from
`GET /api/v1/agent/edge-config`. Field names + defaults MUST match
`sentry_agent_pc.edge.config.EdgeConfig` (the agent's `from_dict` ignores unknown
keys and keeps its own defaults for missing ones, so drift is silent — keep them
in sync). `version` lets the agent re-apply only when the config actually changes.
"""

from pydantic import BaseModel, Field


class EdgeConfigPayload(BaseModel):
    """The 24 edge tunables + a monotonic version. v1 serves the defaults below;
    a per-store override + version bump arrives with the superadmin EdgeConfig CRUD."""

    # Bumped whenever the served config changes; the agent re-applies on change.
    version: int = 1

    # --- Detection ---
    person_conf: float = 0.35
    item_conf: float = 0.40
    frame_skip: int = 3
    # --- Behaviour signal weights + geometry ---
    w_holding: float = 5.0
    w_conceal: float = 14.0
    w_wrist_torso: float = 3.0
    reach_frac: float = 0.35
    near_frac: float = 0.18
    min_kp_conf: float = 0.30
    # --- Risk → episode FSM ---
    decay: float = 0.90
    open_risk: float = 60.0
    close_risk: float = 30.0
    post_quiet_sec: float = 2.0
    drop_after_sec: float = 1.5
    iou_match: float = 0.3
    band_yellow: float = 40.0
    band_red: float = 70.0
    # --- Clip recorder ([-3s .. +3s]) ---
    pre_sec: float = 3.0
    post_sec: float = 3.0
    segment_sec: float = 1.0
    keep_sec: float = 45.0
    max_clips: int = 50
    max_age_sec: float = Field(default=float(7 * 24 * 3600))
    # --- Server handoff ---
    upload_clips: bool = True
