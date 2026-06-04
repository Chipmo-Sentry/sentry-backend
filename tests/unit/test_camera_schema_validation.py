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
