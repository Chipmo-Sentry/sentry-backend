"""Camera (per-store IP camera)."""

import enum
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Float, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ComputeTier(enum.StrEnum):
    """Where this camera's Stage-1 (YOLO + behaviour) runs — the billing + topology
    primitive (ADR-0029). Stored as a plain string column (not a PG enum) so adding
    a tier later needs no type migration.

    - ``cloud`` — sentry-ai on the GPU node runs Stage-1 + Stage-2 from a pulled RTSP
      stream. The default; the existing path, unchanged.
    - ``edge_pc`` — the store PC (sentry-agent-pc) runs Stage-1 locally and uploads
      only suspicious clips for the cloud VLM verdict.
    - ``edge_device`` — a future on-prem edge box (Orange Pi 5, sentry-agent-pi5).
      Reserved; not built yet.
    """

    cloud = "cloud"
    edge_pc = "edge_pc"
    edge_device = "edge_device"


class Camera(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cameras"

    store_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stores.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Encrypted at rest with Fernet — see sentry_backend.security
    rtsp_url_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # {"x": int, "y": int, "w": int, "h": int} — bbox for Stage 1 shelf-zone filter
    shelf_zone_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # DEPRECATED: superseded by `risk_threshold` (0-100, the live-mode breach
    # threshold). Kept only to avoid a migration drop during the pilot; no code
    # reads it. Do NOT add new references — use risk_threshold instead.
    stage2_threshold: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # M1-LIVE L5: maps this Camera row to a sentry-ingest MediaMTX path name
    # (e.g. "cam1_hik") so the live worker can resolve `camera_id` (string from
    # live_metadata) back to the org/store/camera UUID hierarchy.
    mediamtx_path: Mapped[str | None] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=True,
    )
    # Risk threshold (0-100); when a tracked person crosses this in live mode,
    # we cut a clip + run VLM verify. Default 11 = the yellow band — cast a wide
    # net and let the VLM filter (it drops "browsing" → ignore), so anything that
    # turns yellow/red on the live view gets checked. Per-camera tunable in the
    # web UI; raise it if a busy camera generates too much VLM load.
    risk_threshold: Mapped[float] = mapped_column(Float, default=11.0, nullable=False)

    # ADR-0029 — per-camera compute tier (billing + Stage-1 location). Additive;
    # default 'cloud' keeps every existing camera on the unchanged cloud-pull path.
    compute_tier: Mapped[str] = mapped_column(
        String(16),
        default=ComputeTier.cloud.value,
        server_default=ComputeTier.cloud.value,
        nullable=False,
    )
    # docs/29 — per-camera detection zones: a list of normalized polygons
    # [{"id","type","points":[[x,y],...]}], 0-1 image-space coords. Authored on
    # the agent-pc, synced via camera-CRUD, consumed by the behavior engine
    # (exit_after_concealment / repeated_shelf_visit) + drawn read-only on /live.
    # Additive; null = no zones. Distinct shape from shelf_zone_json (single bbox).
    zones: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    @property
    def topology_mode(self) -> str:
        """Derived routing flag: 'cloud' for the cloud tier, 'edge' otherwise.
        Edge cameras skip the cloud live worker + MediaMTX pull (Stage-1 runs on
        the store device); cloud cameras are untouched."""
        return "cloud" if self.compute_tier == ComputeTier.cloud.value else "edge"
