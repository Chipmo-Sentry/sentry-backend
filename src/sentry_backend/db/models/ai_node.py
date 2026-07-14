"""AI compute node (a Predator/Hetzner box running sentry-ai) + pairing codes.

Onboarding mirrors agent pairing, but AI nodes are infrastructure managed by
super-admins (not org-scoped):
  1. Super-admin generates a 6-digit pairing code.
  2. The installed sentry-ai server posts the code → backend creates an AiNode
     row, returns a long-lived ai_node JWT.
  3. The node heartbeats (telemetry) + polls its config; the backend routes
     live-worker start/stop calls to the node's registered public_url.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AiNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_nodes"

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Reachable base URL of this node's sentry-ai (e.g. its cloudflared
    # hostname). The backend POSTs /v1/live/start here.
    public_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gpu: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Latest heartbeat metrics as a JSON string (fps, vram_mb, active_cameras…).
    telemetry: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Centrally-managed config (pushed to the node via GET /ai-nodes/config).
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Default for FRESHLY PAIRED nodes. vLLM is the production standard (owner
    # decision, 2026-07): a replacement node must come up on vLLM without a
    # superadmin dropdown visit. qwen3-vl-4b (Ollama) stays the rollback path.
    provider: Mapped[str] = mapped_column(String(64), default="qwen3-vl-vllm", nullable=False)
    frame_skip: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # Per-node YOLO + scan/VLM tuning (central control, like provider/breach_mode).
    # The node polls these from /ai-nodes/config and hot-applies them to its
    # running camera workers + verify path WITHOUT a restart. Until this shipped
    # these lived only in the node's .env / hardcoded literals, so frame_skip was
    # editable in the UI but never actually consumed; now the whole detection
    # pipeline is operator-tunable from superadmin.
    person_conf: Mapped[float] = mapped_column(Float, default=0.35, nullable=False)
    item_conf: Mapped[float] = mapped_column(Float, default=0.40, nullable=False)
    # Run the COCO item detector every Nth analyzed frame (items move slowly).
    item_every_n: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # Seconds between VLM scan ticks (the node hands the most-suspicious present
    # person to the VLM at most this often).
    scan_interval_sec: Mapped[float] = mapped_column(Float, default=3.0, nullable=False)
    # Keyframes extracted from the breach clip and sent to the VLM per scan.
    frames_per_clip: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Max edge (px) each VLM keyframe is resized to before inference.
    frame_max_dim: Mapped[int] = mapped_column(Integer, default=320, nullable=False)
    # Live-breach topology — the SINGLE source of truth for who creates breach
    # alerts (ADR-0026 central control, like `provider`). The node polls this and
    # hot-applies it; nothing on the node decides it locally any more, so the old
    # node-env / backend-env split (which could double-fire or silently no-op) is
    # gone. "node_push" = node detects → cuts+VLM → POSTs the finished alert
    # (the only live path); "off" = AI runs + overlays risk but creates NO alerts
    # (maintenance). The retired cloud→node backend-pull is not an option here.
    breach_mode: Mapped[str] = mapped_column(String(16), default="node_push", nullable=False)

    paired_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class AiNodePairingCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ai_node_pairing_codes"

    code: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_node_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )


class AiNodeMetric(UUIDPrimaryKeyMixin, Base):
    """One resource-telemetry sample (per heartbeat) — the time-series behind the
    observability dashboard's CPU/RAM/GPU charts (docs/19). One row per beat."""

    __tablename__ = "ai_node_metrics"
    __table_args__ = (Index("ix_ai_node_metrics_node_ts", "ai_node_id", "ts"),)

    ai_node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ai_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ram_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ram_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vram_used_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vram_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_temp_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps_inference: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_cameras: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sentry-project-only (process-scoped) usage, alongside the whole-machine
    # columns above. No sentry_gpu_pct — NVML utilisation is device-wide only.
    sentry_cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentry_ram_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sentry_vram_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
