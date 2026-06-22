"""ADR-0029 — per-camera `compute_tier` (cloud | edge_pc | edge_device) and the
derived `topology_mode`. Additive: default 'cloud' keeps every existing camera on
the unchanged cloud-pull path. These are pure-Python unit tests (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sentry_backend.db.models.camera import Camera, ComputeTier
from sentry_backend.schemas.agent import AgentCameraCreate, AgentCameraUpdate
from sentry_backend.schemas.camera import CameraCreate, CameraPublic, CameraUpdate

_STORE_ID = "00000000-0000-0000-0000-000000000001"


def test_compute_tier_values() -> None:
    assert ComputeTier.cloud == "cloud"
    assert ComputeTier.edge_pc == "edge_pc"
    assert ComputeTier.edge_device == "edge_device"


@pytest.mark.parametrize(
    ("tier", "expected"),
    [("cloud", "cloud"), ("edge_pc", "edge"), ("edge_device", "edge")],
)
def test_topology_mode_derived(tier: str, expected: str) -> None:
    assert Camera(compute_tier=tier).topology_mode == expected


def test_camera_create_defaults_cloud() -> None:
    assert CameraCreate(store_id=_STORE_ID, name="Front").compute_tier == ComputeTier.cloud


def test_camera_create_accepts_edge() -> None:
    c = CameraCreate(store_id=_STORE_ID, name="Front", compute_tier="edge_pc")
    assert c.compute_tier == ComputeTier.edge_pc


def test_camera_create_rejects_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        CameraCreate(store_id=_STORE_ID, name="Front", compute_tier="gpu_farm")


def test_camera_update_tier_optional() -> None:
    assert CameraUpdate().compute_tier is None
    assert CameraUpdate(compute_tier="edge_pc").compute_tier == ComputeTier.edge_pc


def test_agent_schemas_default_and_optional() -> None:
    assert AgentCameraCreate(name="F", rtsp_url="rtsp://x/y").compute_tier == ComputeTier.cloud
    assert AgentCameraUpdate().compute_tier is None


def test_camera_public_exposes_tier_and_topology() -> None:
    cam = Camera(
        id=uuid4(),
        store_id=uuid4(),
        name="Front",
        rtsp_url_encrypted=None,
        shelf_zone_json=None,
        stage2_threshold=0.5,
        enabled=True,
        mediamtx_path="cam1",
        risk_threshold=11.0,
        compute_tier="edge_pc",
    )
    cam.created_at = datetime.now(UTC)
    pub = CameraPublic.from_orm_camera(cam)
    assert pub.compute_tier == ComputeTier.edge_pc
    assert pub.topology_mode == "edge"


def _make_camera(**overrides: object) -> Camera:
    base: dict[str, object] = {
        "id": uuid4(),
        "store_id": uuid4(),
        "name": "Front",
        "rtsp_url_encrypted": None,
        "shelf_zone_json": None,
        "stage2_threshold": 0.5,
        "enabled": True,
        "mediamtx_path": "cam1",
        "risk_threshold": 11.0,
        "compute_tier": "cloud",
    }
    base.update(overrides)
    cam = Camera(**base)
    cam.created_at = datetime.now(UTC)
    return cam


def test_camera_public_round_trips_zones() -> None:
    """from_orm_camera (the single ORM→DTO choke-point) maps stored JSONB zone
    dicts back into typed Zone models (docs/29)."""
    cam = _make_camera(
        zones=[{"id": "z1", "type": "exit", "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}]
    )
    pub = CameraPublic.from_orm_camera(cam)
    assert pub.zones is not None
    assert pub.zones[0].type == "exit"
    assert pub.zones[0].id == "z1"
    assert pub.zones[0].points == [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]


def test_camera_public_zones_none_when_unset() -> None:
    pub = CameraPublic.from_orm_camera(_make_camera(zones=None))
    assert pub.zones is None
