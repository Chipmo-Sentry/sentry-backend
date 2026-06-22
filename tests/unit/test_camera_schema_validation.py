"""mediamtx_path must be a safe slug — it reaches the recordings filesystem
path and the MediaMTX control-API URL, so traversal/injection inputs must be
rejected at the schema boundary for both the user- and agent-facing schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentry_backend.repository.camera_repo import _slugify
from sentry_backend.schemas.agent import AgentCameraCreate
from sentry_backend.schemas.camera import CameraCreate

_STORE_ID = "00000000-0000-0000-0000-000000000001"

BAD_PATHS = [
    "../../etc/passwd",
    "a/b",
    "cam name",  # space
    "../escape",
    "UPPER",  # uppercase not allowed
    "_leading",  # must start alphanumeric
    "x" * 65,  # too long
    "bad..dots",  # '.' not allowed
]

GOOD_PATHS = ["cam1_hik", "store-3-entrance", "a", "cam_2"]


@pytest.mark.parametrize("path", BAD_PATHS)
def test_camera_create_rejects_unsafe_mediamtx_path(path: str) -> None:
    with pytest.raises(ValidationError):
        CameraCreate(store_id=_STORE_ID, name="Front", mediamtx_path=path)


@pytest.mark.parametrize("path", BAD_PATHS)
def test_agent_camera_create_rejects_unsafe_mediamtx_path(path: str) -> None:
    with pytest.raises(ValidationError):
        AgentCameraCreate(name="Front", rtsp_url="rtsp://x/y", mediamtx_path=path)


@pytest.mark.parametrize("path", GOOD_PATHS)
def test_valid_paths_accepted(path: str) -> None:
    assert CameraCreate(store_id=_STORE_ID, name="Front", mediamtx_path=path).mediamtx_path == path


def test_auto_generated_slug_satisfies_pattern() -> None:
    # The auto-generated slug (used when mediamtx_path is None) must itself pass
    # the schema pattern, otherwise re-submitting a generated value would fail.
    import re

    from sentry_backend.schemas.camera import MEDIAMTX_PATH_PATTERN

    for name in ["Front Door", "Касс №2", "  ", "Aisle-7!!!"]:
        assert re.match(MEDIAMTX_PATH_PATTERN, _slugify(name)), name


# === AgentCameraUpdate — partial edit from the desktop agent ===


def test_agent_camera_update_all_optional() -> None:
    """An empty update is valid (no-op); every field defaults to None."""
    from sentry_backend.schemas.agent import AgentCameraUpdate

    u = AgentCameraUpdate()
    assert u.name is None and u.rtsp_url is None and u.risk_threshold is None


def test_agent_camera_update_accepts_partial() -> None:
    from sentry_backend.schemas.agent import AgentCameraUpdate

    u = AgentCameraUpdate(rtsp_url="rtsp://admin:pw@192.168.1.5:554/Streaming/Channels/101")
    assert u.rtsp_url is not None and u.name is None


def test_agent_camera_update_rejects_empty_name_and_url() -> None:
    from sentry_backend.schemas.agent import AgentCameraUpdate

    with pytest.raises(ValidationError):
        AgentCameraUpdate(name="")
    with pytest.raises(ValidationError):
        AgentCameraUpdate(rtsp_url="")


def test_agent_camera_update_risk_threshold_bounds() -> None:
    from sentry_backend.schemas.agent import AgentCameraUpdate

    AgentCameraUpdate(risk_threshold=0.0)
    AgentCameraUpdate(risk_threshold=100.0)
    with pytest.raises(ValidationError):
        AgentCameraUpdate(risk_threshold=-1.0)
    with pytest.raises(ValidationError):
        AgentCameraUpdate(risk_threshold=101.0)


# === Zone — per-camera detection polygons (docs/29) ===

_TRI = [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]  # a valid 3-point normalized polygon


def test_zone_accepts_valid_polygon() -> None:
    from sentry_backend.schemas.camera import Zone

    z = Zone(type="exit", points=_TRI)
    assert z.type == "exit" and len(z.points) == 3 and z.id is None


def test_zone_rejects_fewer_than_three_points() -> None:
    from sentry_backend.schemas.camera import Zone

    with pytest.raises(ValidationError):
        Zone(type="shelf", points=[(0.1, 0.1), (0.9, 0.9)])


@pytest.mark.parametrize(
    "pt",
    [(-0.01, 0.5), (0.5, 1.01), (1.5, 0.5), (0.5, -2.0)],
)
def test_zone_rejects_out_of_range_points(pt: tuple[float, float]) -> None:
    from sentry_backend.schemas.camera import Zone

    with pytest.raises(ValidationError):
        Zone(type="exit", points=[(0.1, 0.1), (0.9, 0.1), pt])


def test_zone_rejects_unknown_type() -> None:
    from sentry_backend.schemas.camera import Zone

    with pytest.raises(ValidationError):
        Zone(type="aisle", points=_TRI)  # type: ignore[arg-type]


def test_zone_boundary_coords_allowed() -> None:
    """0.0 and 1.0 are inclusive bounds — a zone may hug the frame edge."""
    from sentry_backend.schemas.camera import Zone

    Zone(type="entrance", points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])


def test_camera_create_accepts_zones() -> None:
    from sentry_backend.schemas.camera import CameraCreate, Zone

    c = CameraCreate(
        store_id=_STORE_ID,
        name="Front",
        zones=[Zone(type="exit", points=_TRI)],
    )
    assert c.zones is not None and c.zones[0].type == "exit"


def test_camera_create_zones_default_none() -> None:
    from sentry_backend.schemas.camera import CameraCreate

    assert CameraCreate(store_id=_STORE_ID, name="Front").zones is None


def test_agent_camera_create_accepts_zones() -> None:
    from sentry_backend.schemas.agent import AgentCameraCreate
    from sentry_backend.schemas.camera import Zone

    c = AgentCameraCreate(
        name="Front",
        rtsp_url="rtsp://x/y",
        zones=[Zone(type="shelf", points=_TRI)],
    )
    assert c.zones is not None and c.zones[0].type == "shelf"


def test_agent_camera_update_zones_clear_vs_noop() -> None:
    """None = leave zones unchanged (no-op); [] = clear all zones."""
    from sentry_backend.schemas.agent import AgentCameraUpdate

    assert AgentCameraUpdate().zones is None  # no-op
    assert AgentCameraUpdate(zones=[]).zones == []  # explicit clear


def test_camera_create_rejects_bad_zone_in_list() -> None:
    """A malformed polygon anywhere in the list fails the whole payload."""
    from sentry_backend.schemas.camera import CameraCreate, Zone

    with pytest.raises(ValidationError):
        CameraCreate(
            store_id=_STORE_ID,
            name="Front",
            zones=[Zone(type="exit", points=_TRI), {"type": "shelf", "points": [[0.0, 0.0]]}],  # type: ignore[list-item]
        )


# === Upper bounds — a semi-trusted agent must not bloat the JSONB payload ===


def _ramp(n: int) -> list[tuple[float, float]]:
    """n points along the top edge — all in-range, so only the COUNT cap can reject."""
    return [(i / (n + 1), 0.0) for i in range(n)]


def test_zone_accepts_max_points() -> None:
    from sentry_backend.schemas.camera import MAX_ZONE_POINTS, Zone

    Zone(type="exit", points=_ramp(MAX_ZONE_POINTS))  # boundary allowed


def test_zone_rejects_too_many_points() -> None:
    from sentry_backend.schemas.camera import MAX_ZONE_POINTS, Zone

    with pytest.raises(ValidationError):
        Zone(type="exit", points=_ramp(MAX_ZONE_POINTS + 1))


def test_camera_create_rejects_too_many_zones() -> None:
    from sentry_backend.schemas.camera import MAX_ZONES_PER_CAMERA, CameraCreate, Zone

    zones = [Zone(type="shelf", points=_TRI) for _ in range(MAX_ZONES_PER_CAMERA + 1)]
    with pytest.raises(ValidationError):
        CameraCreate(store_id=_STORE_ID, name="Front", zones=zones)


def test_camera_create_accepts_max_zones() -> None:
    from sentry_backend.schemas.camera import MAX_ZONES_PER_CAMERA, CameraCreate, Zone

    zones = [Zone(type="shelf", points=_TRI) for _ in range(MAX_ZONES_PER_CAMERA)]
    assert len(CameraCreate(store_id=_STORE_ID, name="Front", zones=zones).zones or []) == (
        MAX_ZONES_PER_CAMERA
    )


def test_agent_camera_create_rejects_too_many_zones() -> None:
    from sentry_backend.schemas.agent import AgentCameraCreate
    from sentry_backend.schemas.camera import MAX_ZONES_PER_CAMERA, Zone

    zones = [Zone(type="shelf", points=_TRI) for _ in range(MAX_ZONES_PER_CAMERA + 1)]
    with pytest.raises(ValidationError):
        AgentCameraCreate(name="Front", rtsp_url="rtsp://x/y", zones=zones)


def test_agent_camera_update_rejects_too_many_zones() -> None:
    from sentry_backend.schemas.agent import AgentCameraUpdate
    from sentry_backend.schemas.camera import MAX_ZONES_PER_CAMERA, Zone

    zones = [Zone(type="shelf", points=_TRI) for _ in range(MAX_ZONES_PER_CAMERA + 1)]
    with pytest.raises(ValidationError):
        AgentCameraUpdate(zones=zones)
