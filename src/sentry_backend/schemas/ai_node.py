"""AI node schemas — pairing, heartbeat, config, admin views."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_serializer,
)

# A node is "online" if its last heartbeat landed within this window. The
# heartbeat interval is 60s; this tolerates a couple of missed/late beats
# (the heartbeat process can lag under GPU load) without flapping.
NODE_ONLINE_WINDOW = timedelta(minutes=5)


class AiNodePairRequest(BaseModel):
    """Posted by the installed sentry-ai server with the 6-digit code."""

    code: str = Field(min_length=4, max_length=8)
    name: str | None = Field(default=None, max_length=255)
    hostname: str | None = Field(default=None, max_length=255)
    public_url: str | None = Field(default=None, max_length=512)
    version: str | None = Field(default=None, max_length=64)
    gpu: str | None = Field(default=None, max_length=255)


class AiNodeConfig(BaseModel):
    """Config the node polls + applies."""

    enabled: bool
    provider: str
    frame_skip: int
    # Live-breach topology (central control). "node_push" = node detects + cuts +
    # VLM-verifies + POSTs alerts; "off" = AI/overlay only, no alerts.
    breach_mode: str = "node_push"
    # Per-node YOLO + scan/VLM tuning the node hot-applies (central control). All
    # have defaults so an older backend response (without these keys) leaves the
    # node on its built-in defaults.
    person_conf: float = 0.35
    item_conf: float = 0.40
    item_every_n: int = 5
    scan_interval_sec: float = 3.0
    frames_per_clip: int = 1
    frame_max_dim: int = 320
    # Staff badge color (named color or #rrggbb). None → staff identification
    # off on the node. Set by superadmin once the owner picks the lanyard color.
    staff_badge_color: str | None = None


class AiNodePairResult(BaseModel):
    ai_node_token: str
    ai_node_id: UUID
    config: AiNodeConfig


class CameraHealth(BaseModel):
    """Per-camera stream health inside the node heartbeat (audit T12 #3).

    Lets the cloud tell WHICH camera died instead of only seeing the summed
    FPS dip. status: ok = inferring, stalled = worker alive but 0 FPS,
    error = worker dead or RTSP read/open failure.
    """

    camera_id: str = Field(min_length=1, max_length=64)
    fps: float | None = Field(default=None, ge=0)
    status: Literal["ok", "stalled", "error"] = "ok"


def parse_camera_health(telemetry: str | None) -> list[CameraHealth] | None:
    """Extract the `cameras` list from a stored telemetry JSON string.

    None when telemetry is missing, malformed, or from an old node version that
    doesn't report per-camera health yet. Shared by the admin API (AiNodePublic)
    and the node watchdog so both read the same shape.
    """
    if not telemetry:
        return None
    try:
        data = json.loads(telemetry)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cams = data.get("cameras")
    if not isinstance(cams, list):
        return None
    try:
        return [CameraHealth.model_validate(c) for c in cams]
    except ValidationError:
        return None


def parse_served_paths(telemetry: str | None) -> list[str]:
    """MediaMTX paths the node reports it is currently serving (ready streams).
    Lets the backend keep the node↔camera link + HLS proxy working when the node
    runs no analysis worker (edge-first, docs/32 P3). [] for old nodes."""
    if not telemetry:
        return []
    try:
        data = json.loads(telemetry)
    except ValueError:
        return []
    paths = data.get("mediamtx_paths") if isinstance(data, dict) else None
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def parse_hls_base(telemetry: str | None) -> str | None:
    """The node's externally-reachable MediaMTX HLS base from its telemetry JSON,
    used by the live HLS proxy. None for old nodes / no telemetry."""
    if not telemetry:
        return None
    try:
        data = json.loads(telemetry)
    except ValueError:
        return None
    base = data.get("mediamtx_hls_base") if isinstance(data, dict) else None
    return base if isinstance(base, str) and base.startswith(("http://", "https://")) else None


def parse_whep_base(telemetry: str | None) -> str | None:
    """The node's externally-reachable MediaMTX WHEP base (WebRTC egress) from
    its telemetry JSON — handed to the browser so live view can play sub-second
    WebRTC instead of HLS. None for old nodes / no telemetry."""
    if not telemetry:
        return None
    try:
        data = json.loads(telemetry)
    except ValueError:
        return None
    base = data.get("mediamtx_whep_base") if isinstance(data, dict) else None
    return base if isinstance(base, str) and base.startswith(("http://", "https://")) else None


class ComponentUsage(BaseModel):
    """Per-component CPU/RAM (sentry-ai / Ollama / MediaMTX / Tunnel) — the
    resource breakdown shown in the dashboard. Stored in telemetry JSON."""

    name: str = Field(max_length=32)
    cpu_pct: float | None = Field(default=None, ge=0)
    ram_mb: int | None = Field(default=None, ge=0)


class VlmStatus(BaseModel):
    """Whether a VLM model is resident in Ollama + its GPU split, so the dashboard
    can show the VLM's real GPU/VRAM footprint (per-process VRAM isn't on WDDM)."""

    loaded: bool
    model: str | None = Field(default=None, max_length=64)
    vram_mb: int | None = Field(default=None, ge=0)
    gpu_pct: int | None = Field(default=None, ge=0, le=100)


class VlmActivity(BaseModel):
    """VLM verify run history — proves the VLM HAS run on the GPU even though it's
    event-driven (breach-only) and idle most of the time."""

    count: int = Field(default=0, ge=0)
    last_ago_sec: int | None = Field(default=None, ge=0)
    last_latency_ms: int | None = Field(default=None, ge=0)


class AiNodeHeartbeat(BaseModel):
    """Telemetry the node reports every ~60s."""

    fps_inference: float | None = None
    vram_mb: int | None = Field(default=None, ge=0)  # legacy alias; prefer vram_used_mb below
    active_cameras: int | None = None
    version: str | None = Field(default=None, max_length=64)
    # YOLO pose model the live worker runs (e.g. "yolo26s-pose") — surfaced so the
    # dashboard shows whether YOLO26 or YOLO11 is active.
    yolo_model: str | None = Field(default=None, max_length=48)
    # Per-dependency health the node probes locally (e.g. {"ollama": true,
    # "ingest": false, "ai": true}). Opaque map so the node can add keys
    # without a schema change; surfaced read-only in superadmin.
    health: dict[str, bool] | None = None
    # Whether the effective VLM provider actually needs Ollama. False on a vLLM
    # node, so the dashboard renders the (expected) Ollama-down dot neutral instead
    # of a red alarm. Stored in telemetry JSON; surfaced read-only in superadmin.
    ollama_required: bool | None = None
    # Externally-reachable MediaMTX HLS base (e.g. http://1.2.3.4:23289), self-
    # derived from the node's vast.ai env. The backend proxies live HLS to it over
    # HTTPS so the frontend has no mixed-content / hardcoded-ephemeral-port issue.
    mediamtx_hls_base: str | None = Field(default=None, max_length=200)
    # Externally-reachable MediaMTX WHEP base (WebRTC egress, port 8889) — same
    # derivation rules as the HLS base. Handed to the browser via the stream-token
    # endpoint so live view plays sub-second WebRTC with HLS as fallback.
    mediamtx_whep_base: str | None = Field(default=None, max_length=200)
    # MediaMTX paths the node is currently serving (ready streams) — keeps the
    # node↔camera link + HLS proxy working in edge-first mode where the node runs
    # no analysis worker (so no per-camera health). docs/32 P3.
    mediamtx_paths: list[str] | None = Field(default=None, max_length=64)
    # Resource load for the observability dashboard (docs/19). GPU fields are
    # None on a CPU-only node.
    cpu_pct: float | None = Field(default=None, ge=0, le=100)
    ram_used_mb: int | None = Field(default=None, ge=0)
    ram_total_mb: int | None = Field(default=None, ge=0)
    gpu_pct: int | None = Field(default=None, ge=0, le=100)
    vram_used_mb: int | None = Field(default=None, ge=0)
    vram_total_mb: int | None = Field(default=None, ge=0)
    gpu_temp_c: int | None = None
    # Sentry-project-only (process-scoped) usage — the project's own footprint,
    # vs the whole-machine fields above. No sentry_gpu_pct (NVML utilisation is
    # device-wide only; only VRAM is per-process).
    sentry_cpu_pct: float | None = Field(default=None, ge=0, le=100)
    sentry_ram_mb: int | None = Field(default=None, ge=0)
    sentry_vram_mb: int | None = Field(default=None, ge=0)
    # Per-camera stream health (audit T12 #3). Optional: old node versions don't
    # send it. Stored inside `ai_nodes.telemetry` JSON with the rest of the body
    # (no new table/migration); length-capped to keep that Text column bounded.
    cameras: list[CameraHealth] | None = Field(default=None, max_length=64)
    # Resource breakdown (per-component CPU/RAM) + VLM GPU residency, shown in the
    # dashboard's expanded metrics panel. Stored in telemetry JSON.
    components: list[ComponentUsage] | None = Field(default=None, max_length=12)
    vlm: VlmStatus | None = None
    vlm_activity: VlmActivity | None = None
    # Central-control feedback (ADR-0026): the VLM provider the node ACTUALLY uses
    # (effective), whether its model is reachable, and an error if not. Lets the
    # dashboard show applied-on-server vs applying vs error next to the desired
    # provider. Stored in telemetry JSON; does NOT overwrite the desired provider.
    provider_effective: str | None = Field(default=None, max_length=64)
    provider_ready: bool | None = None
    provider_error: str | None = Field(default=None, max_length=300)
    # Central-control feedback for the live-breach topology: the breach_mode the
    # node ACTUALLY applied (effective), so the dashboard can show applied vs
    # applying next to the desired breach_mode. Stored in telemetry JSON.
    breach_mode_effective: str | None = Field(default=None, max_length=16)


class AiNodePairingCodePublic(BaseModel):
    code: str
    expires_at: datetime


class AiNodePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str | None
    hostname: str | None
    public_url: str | None
    version: str | None
    gpu: str | None
    is_active: bool
    last_seen_at: datetime | None
    telemetry: str | None
    enabled: bool
    provider: str
    frame_skip: int
    breach_mode: str
    # Per-node YOLO + scan/VLM tuning (central control). Surfaced so the superadmin
    # node dialog can pre-fill them on edit.
    person_conf: float
    item_conf: float
    item_every_n: int
    scan_interval_sec: float
    frames_per_clip: int
    frame_max_dim: int
    created_at: datetime

    @field_serializer("last_seen_at", "created_at")
    def _ser_dt(self, v: datetime | None) -> str | None:
        # Always emit a tz-aware ISO string so the browser never parses a naive
        # UTC timestamp as local time (which made the node look hours stale →
        # permanently "offline" in UTC+ timezones).
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.isoformat()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cameras(self) -> list[CameraHealth] | None:
        """Per-camera stream health parsed from the latest heartbeat telemetry
        (audit T12 #3). None for old node versions / no telemetry yet — the
        superadmin UI can distinguish "unknown" from "no cameras" ([])."""
        return parse_camera_health(self.telemetry)

    def _telemetry_obj(self) -> dict[str, object] | None:
        if not self.telemetry:
            return None
        try:
            data = json.loads(self.telemetry)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provider_effective(self) -> str | None:
        """VLM provider the node actually runs (from its last heartbeat). The UI
        compares this to `provider` (desired) to show applied vs applying."""
        obj = self._telemetry_obj()
        v = obj.get("provider_effective") if obj else None
        return v if isinstance(v, str) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provider_ready(self) -> bool | None:
        obj = self._telemetry_obj()
        v = obj.get("provider_ready") if obj else None
        return v if isinstance(v, bool) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provider_error(self) -> str | None:
        """Why the effective provider isn't ready (e.g. model not pulled), or None."""
        obj = self._telemetry_obj()
        v = obj.get("provider_error") if obj else None
        return v if isinstance(v, str) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def breach_mode_effective(self) -> str | None:
        """Live-breach topology the node actually applied (from its last
        heartbeat). The UI compares this to `breach_mode` (desired) to show
        applied vs applying. None for old node versions / no telemetry yet."""
        obj = self._telemetry_obj()
        v = obj.get("breach_mode_effective") if obj else None
        return v if isinstance(v, str) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_online(self) -> bool:
        """Server-computed online status — independent of the viewer's clock."""
        if self.last_seen_at is None:
            return False
        ls = self.last_seen_at
        if ls.tzinfo is None:
            ls = ls.replace(tzinfo=UTC)
        return datetime.now(UTC) - ls < NODE_ONLINE_WINDOW


class OrgNodePublic(BaseModel):
    """Org-scoped, camera-PROJECTED view of an AI node for the customer app.

    AiNode has no organization_id — a node serves cameras across orgs and is
    linked to an org ONLY through telemetry.cameras[].camera_id == Camera
    .mediamtx_path. So this view (built by `build_org_node`) deliberately:
      • returns ONLY the caller-org's cameras out of a (possibly shared) node,
      • NEVER exposes the raw telemetry string (it carries other tenants'
        camera ids) — only the safe whole-node gauges are projected through.
    Contrast AiNodePublic (superadmin), which returns everything verbatim.
    """

    id: UUID
    name: str | None
    is_online: bool
    last_seen_at: str | None
    version: str | None
    gpu: str | None
    # Central-control: desired (DB) vs effective (last heartbeat) → applied/applying.
    provider: str
    provider_effective: str | None
    provider_ready: bool | None
    provider_error: str | None
    breach_mode: str
    breach_mode_effective: str | None
    # Whole-node gauges (safe to share — not per-camera/per-tenant).
    fps_inference: float | None
    active_cameras: int | None
    cpu_pct: float | None
    ram_used_mb: int | None
    ram_total_mb: int | None
    gpu_pct: int | None
    vram_used_mb: int | None
    vram_total_mb: int | None
    gpu_temp_c: int | None
    vlm_activity: VlmActivity | None
    vlm: VlmStatus | None
    health: dict[str, bool] | None
    # Per-camera health — FILTERED to the caller-org's mediamtx_paths only.
    cameras: list[CameraHealth]


def _iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.isoformat()


def build_org_node(node: "AiNodeLike", allowed_paths: set[str]) -> OrgNodePublic | None:
    """Project an AiNode down to a single org's view.

    Returns None when this node serves NONE of the org's cameras (so a shared
    node never appears for an org that has no camera on it). `allowed_paths` is
    the set of the org's Camera.mediamtx_path values.
    """
    telemetry = node.telemetry
    obj: dict[str, object] = {}
    if telemetry:
        try:
            parsed = json.loads(telemetry)
            if isinstance(parsed, dict):
                obj = parsed
        except ValueError:
            obj = {}

    all_cams = parse_camera_health(telemetry) or []
    org_cams = [c for c in all_cams if c.camera_id in allowed_paths]
    if not org_cams:
        # This node reports no camera that belongs to the caller's org → hide it.
        return None

    def _f(key: str) -> float | None:
        v = obj.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def _i(key: str) -> int | None:
        v = obj.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    def _s(key: str) -> str | None:
        v = obj.get(key)
        return v if isinstance(v, str) else None

    def _b(key: str) -> bool | None:
        v = obj.get(key)
        return v if isinstance(v, bool) else None

    vlm_activity = None
    if isinstance(obj.get("vlm_activity"), dict):
        try:
            vlm_activity = VlmActivity.model_validate(obj["vlm_activity"])
        except ValidationError:
            vlm_activity = None
    vlm = None
    if isinstance(obj.get("vlm"), dict):
        try:
            vlm = VlmStatus.model_validate(obj["vlm"])
        except ValidationError:
            vlm = None
    health = obj.get("health")
    health_map = (
        {k: bool(v) for k, v in health.items() if isinstance(v, bool)}
        if isinstance(health, dict)
        else None
    )

    ls = node.last_seen_at
    if ls is not None and ls.tzinfo is None:
        ls = ls.replace(tzinfo=UTC)
    is_online = ls is not None and (datetime.now(UTC) - ls) < NODE_ONLINE_WINDOW

    return OrgNodePublic(
        id=node.id,
        name=node.name,
        is_online=is_online,
        last_seen_at=_iso(node.last_seen_at),
        version=node.version,
        gpu=node.gpu,
        provider=node.provider,
        provider_effective=_s("provider_effective"),
        provider_ready=_b("provider_ready"),
        provider_error=_s("provider_error"),
        breach_mode=node.breach_mode,
        breach_mode_effective=_s("breach_mode_effective"),
        fps_inference=_f("fps_inference"),
        active_cameras=_i("active_cameras"),
        cpu_pct=_f("cpu_pct"),
        ram_used_mb=_i("ram_used_mb"),
        ram_total_mb=_i("ram_total_mb"),
        gpu_pct=_i("gpu_pct"),
        vram_used_mb=_i("vram_used_mb"),
        vram_total_mb=_i("vram_total_mb"),
        gpu_temp_c=_i("gpu_temp_c"),
        vlm_activity=vlm_activity,
        vlm=vlm,
        health=health_map,
        cameras=org_cams,
    )


class AiNodeLike(Protocol):
    """Structural type for build_org_node — the AiNode ORM row satisfies it."""

    id: UUID
    name: str | None
    version: str | None
    gpu: str | None
    provider: str
    breach_mode: str
    last_seen_at: datetime | None
    telemetry: str | None


class AiNodeUpdate(BaseModel):
    """Super-admin config edit."""

    name: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=64)
    frame_skip: int | None = Field(default=None, ge=0, le=30)
    breach_mode: Literal["node_push", "off"] | None = None
    # Per-node YOLO + scan/VLM tuning. Ranges keep an operator edit inside sane,
    # GPU-safe bounds (conf as a probability; cadences/sizes positive).
    person_conf: float | None = Field(default=None, ge=0.05, le=0.95)
    item_conf: float | None = Field(default=None, ge=0.05, le=0.95)
    item_every_n: int | None = Field(default=None, ge=1, le=30)
    scan_interval_sec: float | None = Field(default=None, ge=0.5, le=60.0)
    frames_per_clip: int | None = Field(default=None, ge=1, le=8)
    frame_max_dim: int | None = Field(default=None, ge=160, le=1280)
    # Staff badge color (named color or #rrggbb). Empty string clears it (staff
    # identification off); None = leave unchanged.
    staff_badge_color: str | None = Field(default=None, max_length=16)
