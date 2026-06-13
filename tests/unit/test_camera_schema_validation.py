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
