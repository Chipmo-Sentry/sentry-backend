"""Camera CRUD schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from sentry_backend.db.models.camera import Camera, ComputeTier

# mediamtx_path flows into filesystem paths (clip_cutter recordings root) and
# the MediaMTX control-API URL, so it MUST be a safe slug — lowercase
# alphanumerics, underscore and hyphen only, no '/', '..', whitespace, etc.
# Shared by CameraCreate/CameraUpdate and the agent-facing AgentCameraCreate.
MEDIAMTX_PATH_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

# Upper bounds on the per-camera `zones` payload (defense-in-depth). The agent is
# only a semi-trusted client — its JWT derives from a 6-digit store-pairing code —
# so a buggy/malicious agent could otherwise POST one polygon of millions of
# in-range points, or thousands of zones, bloating the JSONB column and amplifying
# memory on EVERY camera-list read (from_orm re-validates each zone). These caps
# are generous for any real floor plan (a store has a handful of zones, a hand-drawn
# polygon a few dozen vertices) while keeping the field bounded like every other
# client-supplied field (mediamtx_path regex, name=255, rtsp_url=2048).
MAX_ZONES_PER_CAMERA = 64
MAX_ZONE_POINTS = 512


class Zone(BaseModel):
    """A per-camera detection zone (docs/29) — a polygon in NORMALIZED image space
    (every coord in 0-1), drawn on the agent-pc and consumed by the behavior
    engine (exit_after_concealment / repeated_shelf_visit). Normalized so one
    definition works across the agent's draw resolution and the cloud worker's
    stream size. Distinct from the deprecated single-bbox `shelf_zone_json`."""

    id: str | None = None
    # `furniture` is tolerated for version-skew safety (an agent older than
    # v0.7.88 derives EVERY plan fixture into zones — rejecting it would 422
    # the whole calibration save). Engines ignore it (edge/zones _VALID_TYPES).
    type: Literal["exit", "shelf", "checkout", "entrance", "furniture", "fridge", "mannequin"]
    points: list[tuple[float, float]]

    @field_validator("points")
    @classmethod
    def _valid_polygon(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(v) < 3:
            raise ValueError("Зон нь дор хаяж 3 цэгтэй байх ёстой.")
        if len(v) > MAX_ZONE_POINTS:
            raise ValueError(f"Зон хэт олон цэгтэй байна (дээд тал {MAX_ZONE_POINTS}).")
        for x, y in v:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("Зоны цэг 0-1 хооронд нормчилогдсон байх ёстой.")
        return v


class CameraCreate(BaseModel):
    store_id: UUID
    name: str = Field(min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    shelf_zone_json: dict[str, Any] | None = None
    stage2_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    enabled: bool = True
    # L5 live-pipeline registration (used by sentry-agent-pc).
    # mediamtx_path is unique per Camera and drives the live-worker camera_id;
    # if None, backend auto-generates a slug from `name`.
    mediamtx_path: str | None = Field(default=None, pattern=MEDIAMTX_PATH_PATTERN)
    # Default 11 = yellow band (matches the Camera model + the agent): cast a wide
    # net and let the VLM filter "browsing". Per-camera tunable in the web UI.
    risk_threshold: float = Field(default=11.0, ge=0.0, le=100.0)
    # ADR-0029 — compute tier (cloud | edge_pc | edge_device); default cloud keeps
    # the existing cloud-pull path. Edge tiers skip the cloud live worker.
    compute_tier: ComputeTier = ComputeTier.cloud
    # docs/29 — per-camera detection zones (normalized polygons).
    zones: list[Zone] | None = Field(default=None, max_length=MAX_ZONES_PER_CAMERA)


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    shelf_zone_json: dict[str, Any] | None = None
    stage2_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool | None = None
    mediamtx_path: str | None = Field(default=None, pattern=MEDIAMTX_PATH_PATTERN)
    risk_threshold: float | None = Field(default=None, ge=0.0, le=100.0)
    compute_tier: ComputeTier | None = None
    # docs/29 — None = no-op (leave zones unchanged); [] = clear all zones.
    zones: list[Zone] | None = Field(default=None, max_length=MAX_ZONES_PER_CAMERA)


class StreamTokenResponse(BaseModel):
    """Short-lived per-camera WHEP/HLS read token (appended as ?jwt=…)."""

    token: str
    expires_in: int
    # Same-origin HTTPS HLS URL proxied through the backend to the camera's node
    # (avoids mixed-content + the node's ephemeral address). The frontend prefers
    # this over any build-time MediaMTX base.
    hls_url: str | None = None
    # Absolute HTTPS WHEP URL on the serving node (jwt appended) — sub-second
    # WebRTC playback. None when the node reports no HTTPS WHEP base; the
    # frontend then stays on the HLS path above.
    whep_url: str | None = None


class CameraPublic(BaseModel):
    """Public Camera — `rtsp_url` NEVER exposed; only a boolean `has_rtsp_url`."""

    id: UUID
    store_id: UUID
    name: str
    shelf_zone_json: dict[str, Any] | None
    stage2_threshold: float
    enabled: bool
    has_rtsp_url: bool
    created_at: datetime
    # L5 live-pipeline fields
    mediamtx_path: str | None = None
    risk_threshold: float = 11.0
    # ADR-0029 — compute tier + the derived routing flag (cloud | edge).
    compute_tier: ComputeTier = ComputeTier.cloud
    topology_mode: str = "cloud"
    # docs/29 — per-camera detection zones (normalized polygons), or None.
    zones: list[Zone] | None = None

    @classmethod
    def from_orm_camera(cls, camera: Camera) -> "CameraPublic":
        return cls(
            id=camera.id,
            store_id=camera.store_id,
            name=camera.name,
            shelf_zone_json=camera.shelf_zone_json,
            stage2_threshold=camera.stage2_threshold,
            enabled=camera.enabled,
            has_rtsp_url=camera.rtsp_url_encrypted is not None,
            created_at=camera.created_at,
            mediamtx_path=camera.mediamtx_path,
            risk_threshold=camera.risk_threshold,
            compute_tier=ComputeTier(camera.compute_tier),
            topology_mode=camera.topology_mode,
            zones=[Zone.model_validate(z) for z in camera.zones] if camera.zones else None,
        )
