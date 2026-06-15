"""Per-camera stream health (audit T12 #3) — heartbeat schema, telemetry parsing,
admin exposure via AiNodePublic, and watchdog camera transitions. No DB."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sentry_backend.schemas.ai_node import (
    AiNodeHeartbeat,
    AiNodePublic,
    CameraHealth,
    parse_camera_health,
)
from sentry_backend.services.node_watchdog import (
    CameraKey,
    _format_camera_down,
    _format_camera_recovered,
    detect_camera_transitions,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
THRESHOLD = 180


# --- heartbeat schema validation ------------------------------------------


def test_heartbeat_without_cameras_still_valid() -> None:
    # Old agent → new backend: the field is optional, nothing breaks.
    hb = AiNodeHeartbeat(fps_inference=5.0, active_cameras=2)
    assert hb.cameras is None
    assert "cameras" not in hb.model_dump(exclude_none=True)


def test_heartbeat_with_empty_cameras_list() -> None:
    hb = AiNodeHeartbeat(cameras=[])
    assert hb.cameras == []


def test_heartbeat_with_cameras_parses_and_serializes() -> None:
    hb = AiNodeHeartbeat.model_validate(
        {
            "fps_inference": 5.0,
            "cameras": [
                {"camera_id": "cam-a", "fps": 4.8, "status": "ok"},
                {"camera_id": "cam-b", "fps": 0.0, "status": "error"},
            ],
        }
    )
    assert hb.cameras is not None
    assert hb.cameras[1].status == "error"
    # Round-trips through the json.dumps(model_dump(...)) telemetry storage path.
    telemetry = json.dumps(hb.model_dump(exclude_none=True))
    assert parse_camera_health(telemetry) == hb.cameras


def test_heartbeat_rejects_negative_fps() -> None:
    with pytest.raises(ValidationError):
        AiNodeHeartbeat(cameras=[CameraHealth(camera_id="c", fps=-1.0)])


def test_heartbeat_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        AiNodeHeartbeat.model_validate({"cameras": [{"camera_id": "c", "status": "meltdown"}]})


def test_heartbeat_rejects_oversized_camera_list() -> None:
    cams = [{"camera_id": f"cam-{i}", "fps": 1.0, "status": "ok"} for i in range(65)]
    with pytest.raises(ValidationError):
        AiNodeHeartbeat.model_validate({"cameras": cams})


# --- parse_camera_health ----------------------------------------------------


@pytest.mark.parametrize(
    "telemetry",
    [
        None,
        "",
        "not json",
        "[1, 2]",  # JSON but not an object
        json.dumps({"fps_inference": 5.0}),  # old node: no cameras key
        json.dumps({"cameras": "oops"}),  # wrong type
        json.dumps({"cameras": [{"fps": 1.0}]}),  # entry missing camera_id
    ],
)
def test_parse_camera_health_returns_none_on_bad_input(telemetry: str | None) -> None:
    assert parse_camera_health(telemetry) is None


def test_parse_camera_health_valid() -> None:
    telemetry = json.dumps({"cameras": [{"camera_id": "cam-a", "fps": 3.2, "status": "stalled"}]})
    cams = parse_camera_health(telemetry)
    assert cams == [CameraHealth(camera_id="cam-a", fps=3.2, status="stalled")]


# --- AiNodePublic exposes parsed cameras to the admin API -------------------


def _public_node(telemetry: str | None) -> AiNodePublic:
    return AiNodePublic(
        id=uuid4(),
        name="Predator-ChipmoAI",
        hostname="predator",
        public_url=None,
        version="0.5.0",
        gpu="RTX",
        is_active=True,
        last_seen_at=NOW,
        telemetry=telemetry,
        enabled=True,
        provider="minicpm",
        frame_skip=3,
        created_at=NOW - timedelta(days=1),
    )


def test_public_node_serializes_cameras() -> None:
    telemetry = json.dumps({"cameras": [{"camera_id": "cam-a", "fps": 4.8, "status": "ok"}]})
    data = _public_node(telemetry).model_dump()
    assert data["cameras"] == [{"camera_id": "cam-a", "fps": 4.8, "status": "ok"}]


def test_public_node_cameras_none_for_old_telemetry() -> None:
    assert _public_node(json.dumps({"fps_inference": 5.0})).model_dump()["cameras"] is None
    assert _public_node(None).model_dump()["cameras"] is None


# --- watchdog: per-camera transitions ---------------------------------------


@dataclass
class FakeNode:
    telemetry: str | None
    last_seen_at: datetime | None = NOW
    name: str | None = "Predator-ChipmoAI"
    hostname: str | None = "predator"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = NOW - timedelta(days=30)


def _telemetry(*cams: tuple[str, float, str]) -> str:
    return json.dumps({"cameras": [{"camera_id": c, "fps": f, "status": s} for c, f, s in cams]})


def _step(
    nodes: list[FakeNode], candidates: set[CameraKey], notified: set[CameraKey]
) -> tuple[list[str], list[str]]:
    down, up = detect_camera_transitions(
        nodes, candidates, notified, now=NOW, offline_after_sec=THRESHOLD
    )
    return [c.camera_id for _, c in down], [c.camera_id for _, c in up]


def test_camera_down_debounced_then_notifies_once() -> None:
    node = FakeNode(telemetry=_telemetry(("cam-a", 0.0, "error")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()

    # Tick 1: candidate only (debounce — could be a worker warming up).
    assert _step([node], candidates, notified) == ([], [])
    assert (node.id, "cam-a") in candidates

    # Tick 2: still down → notify.
    assert _step([node], candidates, notified) == (["cam-a"], [])
    assert (node.id, "cam-a") in notified

    # Tick 3: still down → no duplicate.
    assert _step([node], candidates, notified) == ([], [])


def test_transient_blip_never_notifies() -> None:
    node = FakeNode(telemetry=_telemetry(("cam-a", 0.0, "stalled")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    _step([node], candidates, notified)

    # Recovered before the second tick → candidate cleared, no alert ever.
    node.telemetry = _telemetry(("cam-a", 4.8, "ok"))
    assert _step([node], candidates, notified) == ([], [])
    assert candidates == set() and notified == set()


def test_camera_recovery_notifies_and_resets() -> None:
    node = FakeNode(telemetry=_telemetry(("cam-a", 0.0, "error")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    _step([node], candidates, notified)
    _step([node], candidates, notified)  # alerted

    node.telemetry = _telemetry(("cam-a", 4.8, "ok"))
    assert _step([node], candidates, notified) == ([], ["cam-a"])
    assert notified == set()

    # A later outage alerts again (full cycle, with debounce).
    node.telemetry = _telemetry(("cam-a", 0.0, "error"))
    assert _step([node], candidates, notified) == ([], [])
    assert _step([node], candidates, notified) == (["cam-a"], [])


def test_only_down_cameras_alert_on_mixed_node() -> None:
    node = FakeNode(telemetry=_telemetry(("cam-a", 4.8, "ok"), ("cam-b", 0.0, "error")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    _step([node], candidates, notified)
    assert _step([node], candidates, notified) == (["cam-b"], [])


def test_zero_fps_with_ok_status_counts_as_down() -> None:
    # Defensive: a buggy/foreign node claiming "ok" at 0 FPS is still down.
    node = FakeNode(telemetry=_telemetry(("cam-a", 0.0, "ok")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    _step([node], candidates, notified)
    assert _step([node], candidates, notified) == (["cam-a"], [])


def test_offline_node_cameras_are_skipped_but_state_kept() -> None:
    node = FakeNode(telemetry=_telemetry(("cam-a", 0.0, "error")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    _step([node], candidates, notified)
    _step([node], candidates, notified)  # alerted while online

    # Node goes offline → stale telemetry must not produce camera alerts,
    # and the already-notified state survives until the node returns.
    node.last_seen_at = NOW - timedelta(seconds=THRESHOLD + 60)
    assert _step([node], candidates, notified) == ([], [])
    assert (node.id, "cam-a") in notified

    # Node returns with the camera healthy → recovery fires.
    node.last_seen_at = NOW
    node.telemetry = _telemetry(("cam-a", 4.8, "ok"))
    assert _step([node], candidates, notified) == ([], ["cam-a"])


def test_vanished_camera_pruned_without_alert() -> None:
    node = FakeNode(telemetry=_telemetry(("cam-a", 0.0, "error")))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    _step([node], candidates, notified)
    _step([node], candidates, notified)  # alerted

    # Worker stopped on purpose → camera gone from telemetry → state pruned.
    node.telemetry = _telemetry()
    assert _step([node], candidates, notified) == ([], [])
    assert candidates == set() and notified == set()


def test_node_without_camera_telemetry_is_noop() -> None:
    # Old node version (no cameras key) → nothing to evaluate, nothing alerts.
    node = FakeNode(telemetry=json.dumps({"fps_inference": 0.0}))
    candidates: set[CameraKey] = set()
    notified: set[CameraKey] = set()
    assert _step([node], candidates, notified) == ([], [])
    assert _step([node], candidates, notified) == ([], [])


# --- message formatting ------------------------------------------------------


def test_camera_down_message() -> None:
    node = FakeNode(telemetry=None)
    cam = CameraHealth(camera_id="cam-7", fps=0.0, status="error")
    msg = _format_camera_down(node, cam)
    assert "🟡 Камер тасарлаа: cam-7" in msg
    assert "Predator-ChipmoAI" in msg
    assert "error" in msg
    assert "FPS: 0" in msg


def test_camera_down_message_unknown_fps() -> None:
    node = FakeNode(telemetry=None)
    cam = CameraHealth(camera_id="cam-7", fps=None, status="error")
    assert "FPS: ?" in _format_camera_down(node, cam)


def test_camera_recovered_message() -> None:
    node = FakeNode(telemetry=None)
    cam = CameraHealth(camera_id="cam-7", fps=5.0, status="ok")
    msg = _format_camera_recovered(node, cam)
    assert "🟢 Камер сэргэлээ: cam-7" in msg
    assert "Predator-ChipmoAI" in msg
