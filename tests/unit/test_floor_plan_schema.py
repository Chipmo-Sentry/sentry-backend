"""FloorPlan schema (docs/30) — validation, bounds, and JSON round-trip."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentry_backend.schemas.floor_plan import (
    DEFAULT_PLAN_SIZE,
    MAX_CAMERAS,
    MAX_FIXTURES,
    FloorPlan,
)

_TRI = [(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)]
_H = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def test_empty_plan_defaults() -> None:
    p = FloorPlan()
    assert p.version == 1
    assert p.walls == [] and p.fixtures == [] and p.cameras == []
    # Metres — must match the agent editor's DEFAULT_SIZE_M (1 unit == 1 m).
    assert p.size == DEFAULT_PLAN_SIZE == (20.0, 20.0)


def test_furniture_fixture_type_accepted() -> None:
    # Scenery type (буйдан/сандал): drawable + analytics, no zone derivation.
    p = FloorPlan(fixtures=[{"type": "furniture", "points": _TRI}])
    assert p.fixtures[0].type == "furniture"


def test_fixture_label_round_trips() -> None:
    p = FloorPlan(fixtures=[{"type": "shelf", "label": "Архины тавиур", "points": _TRI}])
    back = FloorPlan.model_validate(p.model_dump(mode="json"))
    assert back.fixtures[0].label == "Архины тавиур"
    # Absent label stays None (legacy plans without the field).
    assert FloorPlan(fixtures=[{"type": "shelf", "points": _TRI}]).fixtures[0].label is None


def test_full_plan_validates() -> None:
    p = FloorPlan(
        size=(1200, 900),
        walls=[{"points": [[0, 0], [1200, 0], [1200, 900], [0, 900]]}],
        fixtures=[{"type": "exit", "points": _TRI}, {"type": "shelf", "points": _TRI}],
        cameras=[
            {"camera_id": "cam1", "pos": [100, 200], "dir_deg": 90.0, "homography": _H},
        ],
    )
    assert p.fixtures[0].type == "exit"
    assert p.cameras[0].homography == _H


def test_fixture_needs_three_points() -> None:
    with pytest.raises(ValidationError):
        FloorPlan(fixtures=[{"type": "shelf", "points": [[0, 0], [1, 1]]}])


def test_wall_needs_two_points() -> None:
    with pytest.raises(ValidationError):
        FloorPlan(walls=[{"points": [[0, 0]]}])


def test_unknown_fixture_type_rejected() -> None:
    with pytest.raises(ValidationError):
        FloorPlan(fixtures=[{"type": "aisle", "points": _TRI}])  # type: ignore[list-item]


def test_homography_must_be_3x3() -> None:
    with pytest.raises(ValidationError):
        FloorPlan(cameras=[{"camera_id": "c", "pos": [0, 0], "homography": [[1, 0], [0, 1]]}])


def test_homography_optional() -> None:
    c = FloorPlan(cameras=[{"camera_id": "c", "pos": [0, 0]}]).cameras[0]
    assert c.homography is None and c.dir_deg == 0.0


def test_too_many_fixtures_rejected() -> None:
    fixtures = [{"type": "shelf", "points": _TRI} for _ in range(MAX_FIXTURES + 1)]
    with pytest.raises(ValidationError):
        FloorPlan(fixtures=fixtures)


def test_too_many_cameras_rejected() -> None:
    cams = [{"camera_id": f"c{i}", "pos": [0, 0]} for i in range(MAX_CAMERAS + 1)]
    with pytest.raises(ValidationError):
        FloorPlan(cameras=cams)


def test_json_round_trip() -> None:
    """model_dump(mode='json') → store in JSONB → model_validate back (the GET/PATCH path)."""
    p = FloorPlan(
        fixtures=[{"id": "z1", "type": "exit", "points": _TRI}],
        cameras=[{"camera_id": "cam1", "pos": [10, 20], "homography": _H, "reproj_err": 2.5}],
    )
    dumped = p.model_dump(mode="json")
    assert isinstance(dumped["fixtures"][0]["points"][0], list)  # tuple → list for JSONB
    back = FloorPlan.model_validate(dumped)
    assert back.fixtures[0].id == "z1"
    assert back.cameras[0].homography == _H
    assert back == p
