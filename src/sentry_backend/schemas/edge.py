"""Edge Stage-1 contract schemas (ADR-0029).

`EdgeConfigPayload` is the tunables the store agent's config-poller fetches from
`GET /api/v1/agent/edge-config`. Field names + defaults MUST match
`sentry_agent_pc.edge.config.EdgeConfig` (the agent's `from_dict` ignores unknown
keys and keeps its own defaults for missing ones, so drift is silent — keep them
in sync). `version` lets the agent re-apply only when the config actually changes.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    # docs/29 zone behaviours (must match sentry_agent_pc.edge.config.EdgeConfig)
    w_exit_after_conceal: float = 40.0
    w_repeated_shelf: float = 3.0
    repeated_shelf_threshold: int = 3
    # Per-behaviour timing gates (must match EdgeConfig): bank a behaviour only
    # after it's active >= mindur sec, then once per interval sec. 0 = every frame.
    interval_holding: float = 0.0
    mindur_holding: float = 0.0
    interval_wrist_torso: float = 0.0
    mindur_wrist_torso: float = 0.0
    interval_conceal: float = 0.0
    mindur_conceal: float = 0.0
    interval_repeated_shelf: float = 0.0
    mindur_repeated_shelf: float = 0.0
    interval_exit_after_conceal: float = 0.0
    mindur_exit_after_conceal: float = 0.0
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


# The tunable field names (everything except the monotonic version) — used to
# filter stored overrides on read so a stale key can't reach the constructor.
_TUNABLE_FIELDS = set(EdgeConfigPayload.model_fields) - {"version"}


class EdgeConfigOverridesIn(BaseModel):
    """Superadmin write body (I3/I9): a PARTIAL set of edge tunables. Unset fields
    (None) are left at the agent default; unknown keys are rejected. Stored as the
    per-store ``overrides`` dict via ``model_dump(exclude_none=True)``."""

    model_config = ConfigDict(extra="forbid")

    person_conf: float | None = None
    item_conf: float | None = None
    frame_skip: int | None = None
    w_holding: float | None = None
    w_conceal: float | None = None
    w_wrist_torso: float | None = None
    w_exit_after_conceal: float | None = None
    w_repeated_shelf: float | None = None
    repeated_shelf_threshold: int | None = None
    interval_holding: float | None = None
    mindur_holding: float | None = None
    interval_wrist_torso: float | None = None
    mindur_wrist_torso: float | None = None
    interval_conceal: float | None = None
    mindur_conceal: float | None = None
    interval_repeated_shelf: float | None = None
    mindur_repeated_shelf: float | None = None
    interval_exit_after_conceal: float | None = None
    mindur_exit_after_conceal: float | None = None
    reach_frac: float | None = None
    near_frac: float | None = None
    min_kp_conf: float | None = None
    decay: float | None = None
    open_risk: float | None = None
    close_risk: float | None = None
    post_quiet_sec: float | None = None
    drop_after_sec: float | None = None
    iou_match: float | None = None
    band_yellow: float | None = None
    band_red: float | None = None
    pre_sec: float | None = None
    post_sec: float | None = None
    segment_sec: float | None = None
    keep_sec: float | None = None
    max_clips: int | None = None
    max_age_sec: float | None = None
    upload_clips: bool | None = None


class EdgeConfigAdminView(BaseModel):
    """Superadmin read view: the raw overrides + version + the effective merged
    config the agent would receive."""

    store_id: str
    version: int
    overrides: dict[str, Any]
    updated_at: datetime | None = None
    effective: EdgeConfigPayload


def merged_edge_payload(version: int, overrides: dict[str, Any] | None) -> EdgeConfigPayload:
    """Agent defaults overlaid with the store's overrides, carrying ``version``.

    Unknown keys are dropped and an invalid stored value falls back to defaults
    (with the version preserved) so a hand-edited row can never 500 the agent
    poll — the agent keeps its own defaults for anything missing anyway.
    """
    valid = {k: v for k, v in (overrides or {}).items() if k in _TUNABLE_FIELDS}
    try:
        return EdgeConfigPayload(version=version, **valid)
    except ValueError:
        return EdgeConfigPayload(version=version)
