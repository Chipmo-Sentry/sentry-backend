"""Org-scoped node projection (build_org_node) — the tenant-isolation guard for
the customer Pipeline Canvas / Health views. No DB: build_org_node is pure.

The load-bearing property: AiNode has no organization_id, so a node shared by
two orgs must, per caller-org, return ONLY that org's cameras and NEVER the raw
telemetry string (which carries the other tenant's camera ids)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sentry_backend.schemas.ai_node import OrgNodePublic, build_org_node

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


@dataclass
class FakeNode:
    telemetry: str | None
    last_seen_at: datetime | None = NOW
    name: str | None = "Predator-1"
    version: str | None = "0.5.0"
    gpu: str | None = "RTX 4090"
    provider: str = "qwen3-vl-4b"
    breach_mode: str = "node_push"
    id: UUID = field(default_factory=uuid4)


def _telemetry(cameras: list[tuple[str, float, str]], **extra: object) -> str:
    body: dict[str, object] = {
        "cameras": [{"camera_id": c, "fps": f, "status": s} for c, f, s in cameras]
    }
    body.update(extra)
    return json.dumps(body)


def test_shared_node_returns_only_callers_cameras() -> None:
    # One node serves org-A's cam-a AND org-B's cam-b. Org A must see ONLY cam-a.
    node = FakeNode(telemetry=_telemetry([("cam-a", 4.8, "ok"), ("cam-b", 5.0, "ok")]))
    projected = build_org_node(node, allowed_paths={"cam-a"})
    assert projected is not None
    cam_ids = [c.camera_id for c in projected.cameras]
    assert cam_ids == ["cam-a"]
    assert "cam-b" not in cam_ids


def test_node_with_no_org_cameras_is_hidden() -> None:
    # A node serving only OTHER orgs' cameras must not appear for this org.
    node = FakeNode(telemetry=_telemetry([("cam-x", 4.8, "ok")]))
    assert build_org_node(node, allowed_paths={"cam-a", "cam-b"}) is None


def test_projection_never_exposes_raw_telemetry() -> None:
    node = FakeNode(telemetry=_telemetry([("cam-a", 4.8, "ok"), ("cam-secret", 5.0, "ok")]))
    projected = build_org_node(node, allowed_paths={"cam-a"})
    assert projected is not None
    # The serialized payload must not carry the other tenant's camera id anywhere.
    dumped = json.dumps(projected.model_dump(mode="json"))
    assert "cam-secret" not in dumped
    assert "telemetry" not in projected.model_fields


def test_gauges_and_central_control_projected() -> None:
    node = FakeNode(
        telemetry=_telemetry(
            [("cam-a", 4.8, "ok")],
            fps_inference=9.6,
            active_cameras=2,
            gpu_pct=99,
            vram_used_mb=7400,
            vram_total_mb=8000,
            cpu_pct=42.5,
            provider_effective="qwen3-vl-4b",
            provider_ready=True,
            breach_mode_effective="node_push",
            vlm_activity={"count": 12, "last_ago_sec": 3, "last_latency_ms": 6800},
            vlm={"loaded": True, "model": "qwen3-vl:4b-instruct", "vram_mb": 3900, "gpu_pct": 100},
            health={"ai": True, "ollama": True, "ingest": False},
        )
    )
    p = build_org_node(node, allowed_paths={"cam-a"})
    assert p is not None
    assert p.fps_inference == 9.6
    assert p.gpu_pct == 99
    assert p.provider == "qwen3-vl-4b"  # desired (DB)
    assert p.provider_effective == "qwen3-vl-4b"  # applied (heartbeat)
    assert p.provider_ready is True
    assert p.breach_mode_effective == "node_push"
    assert p.vlm_activity is not None and p.vlm_activity.last_latency_ms == 6800
    assert p.vlm is not None and p.vlm.loaded is True
    assert p.health == {"ai": True, "ollama": True, "ingest": False}


def test_is_online_from_last_seen() -> None:
    # last_seen must be ~now (NODE_ONLINE_WINDOW is 5 min). Using the fixed module
    # NOW constant here was a time-bomb: it only passed within 5 min of that date.
    fresh = FakeNode(
        telemetry=_telemetry([("cam-a", 4.8, "ok")]),
        last_seen_at=datetime.now(UTC),
    )
    assert build_org_node(fresh, {"cam-a"}).is_online is True  # type: ignore[union-attr]

    stale = FakeNode(
        telemetry=_telemetry([("cam-a", 0.0, "error")]),
        last_seen_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    p = build_org_node(stale, {"cam-a"})
    assert p is not None and p.is_online is False


def test_naive_last_seen_treated_as_utc() -> None:
    # A naive datetime (no tzinfo) must not crash or read as wildly stale.
    node = FakeNode(
        telemetry=_telemetry([("cam-a", 4.8, "ok")]),
        last_seen_at=datetime.now(UTC).replace(tzinfo=None),
    )
    p = build_org_node(node, {"cam-a"})
    assert p is not None and p.is_online is True


def test_old_node_without_cameras_key_is_hidden() -> None:
    node = FakeNode(telemetry=json.dumps({"fps_inference": 5.0}))
    assert build_org_node(node, {"cam-a"}) is None


def test_returns_org_node_public_type() -> None:
    node = FakeNode(telemetry=_telemetry([("cam-a", 4.8, "ok")]))
    assert isinstance(build_org_node(node, {"cam-a"}), OrgNodePublic)
