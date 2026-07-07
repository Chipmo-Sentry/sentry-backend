"""Unit tests for the footfall projection (docs/30 F2) — the pure geometry that
turns a normalized camera foot point into a plan grid cell."""

from datetime import UTC, datetime
from uuid import uuid4

from sentry_backend.db.models.analytics_flow import FLOW_GRID
from sentry_backend.db.models.analytics_footfall import GRID_SIZE
from sentry_backend.services.footfall_aggregator import (
    FootfallAggregator,
    _extract_gates,
    _flow_adjacent,
    _foot_norm,
    _hour_bucket,
    _Presence,
    point_in_polygon,
    project_foot_to_cell,
    project_foot_to_plan,
)


def test_no_plan_returns_none() -> None:
    assert project_foot_to_cell(None, "cam-1", 0.5, 0.5) is None
    assert project_foot_to_cell({}, "cam-1", 0.5, 0.5) is None


def test_unknown_camera_returns_none() -> None:
    plan = {"size": [1000, 800], "cameras": [{"camera_id": "cam-1", "pos": [0, 0]}]}
    assert project_foot_to_cell(plan, "other", 0.5, 0.5) is None


def test_identity_homography_maps_norm_to_grid() -> None:
    # H = diag(size) maps normalized (0-1) → plan, so norm passes straight through.
    plan = {
        "size": [1000, 800],
        "cameras": [{"camera_id": "cam-1", "homography": [[1000, 0, 0], [0, 800, 0], [0, 0, 1]]}],
    }
    cell = project_foot_to_cell(plan, "cam-1", 0.5, 0.25)
    assert cell == (int(0.5 * GRID_SIZE), int(0.25 * GRID_SIZE))


def test_homography_clips_out_of_bounds_into_grid() -> None:
    plan = {
        "size": [1000, 800],
        "cameras": [{"camera_id": "cam-1", "homography": [[1000, 0, 0], [0, 800, 0], [0, 0, 1]]}],
    }
    gx, gy = project_foot_to_cell(plan, "cam-1", 5.0, -2.0)  # way off-plan
    assert 0 <= gx < GRID_SIZE
    assert 0 <= gy < GRID_SIZE


def test_fallback_anchors_near_camera_position() -> None:
    # No homography → blob around the camera's placed position. A centered foot
    # point (0.5, 0.5) should land in the cell containing the camera's pos.
    plan = {"size": [1000, 800], "cameras": [{"camera_id": "cam-1", "pos": [500, 400]}]}
    cell = project_foot_to_cell(plan, "cam-1", 0.5, 0.5)
    assert cell == (int(0.5 * GRID_SIZE), int(0.5 * GRID_SIZE))


def test_foot_norm_pixel_box() -> None:
    # Bottom-center of a pixel bbox, normalized by frame size.
    nx, ny = _foot_norm([100, 200, 300, 600], width=1000, height=1200)
    assert abs(nx - 0.2) < 1e-6  # (100+300)/2 / 1000
    assert abs(ny - 0.5) < 1e-6  # 600 / 1200


def test_foot_norm_normalized_box() -> None:
    # Already-normalized box (no frame size) is passed through clamped.
    nx, ny = _foot_norm([0.1, 0.2, 0.5, 0.9], width=0, height=0)
    assert abs(nx - 0.3) < 1e-6
    assert abs(ny - 0.9) < 1e-6


def test_foot_norm_bad_box_returns_none() -> None:
    assert _foot_norm(None, 100, 100) is None
    assert _foot_norm([1, 2, 3], 100, 100) is None


def test_project_foot_to_plan_absolute_coords() -> None:
    plan = {
        "size": [1000, 800],
        "cameras": [{"camera_id": "cam-1", "homography": [[1000, 0, 0], [0, 800, 0], [0, 0, 1]]}],
    }
    result = project_foot_to_plan(plan, "cam-1", 0.5, 0.5)
    assert result is not None
    (px, py), (sx, sy) = result
    assert abs(px - 500) < 1e-6
    assert abs(py - 400) < 1e-6
    assert (sx, sy) == (1000.0, 800.0)


def test_point_in_polygon_square() -> None:
    sq = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert point_in_polygon(5, 5, sq) is True
    assert point_in_polygon(15, 5, sq) is False
    assert point_in_polygon(-1, 5, sq) is False


def test_point_in_polygon_degenerate() -> None:
    assert point_in_polygon(0, 0, [[0, 0], [1, 1]]) is False  # < 3 points


def test_extract_gates_only_entrance_exit() -> None:
    sq = [[0, 0], [10, 0], [10, 10], [0, 10]]
    plan = {
        "fixtures": [
            {"id": "g1", "type": "exit", "points": sq},
            {"id": "e1", "type": "entrance", "points": sq},
            {"id": "s1", "type": "shelf", "points": sq},
            {"id": "c1", "type": "checkout", "points": sq},
        ]
    }
    gates = _extract_gates(plan)
    ids = {g[0] for g in gates}
    assert ids == {"g1", "e1"}


def test_extract_gates_empty() -> None:
    assert _extract_gates(None) == []
    assert _extract_gates({"fixtures": []}) == []


def test_prune_presence_banks_stale_dwell() -> None:
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 6, 13, tzinfo=UTC)
    pres = _Presence(first_mono=100.0, store_id=sid, hour=hour)
    pres.last_mono = 130.0  # 30s span
    agg._presence[("cam", 1)] = pres
    # now=200 → gone 70s ≥ TTL(45) → banked and forgotten.
    agg._prune_presence(200.0)
    assert ("cam", 1) not in agg._presence
    assert agg._dwell_buf[(sid, "cam", hour)] == [1, 30000]


def test_prune_presence_keeps_active_track() -> None:
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 6, 13, tzinfo=UTC)
    pres = _Presence(first_mono=100.0, store_id=sid, hour=hour)
    pres.last_mono = 190.0
    agg._presence[("cam", 2)] = pres
    # now=200 → gone only 10s < TTL → still active, nothing banked.
    agg._prune_presence(200.0)
    assert ("cam", 2) in agg._presence
    assert agg._dwell_buf == {}


def test_prune_presence_accumulates_same_bucket() -> None:
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 6, 13, tzinfo=UTC)
    for pid, span in [(1, 20.0), (2, 40.0)]:
        p = _Presence(first_mono=0.0, store_id=sid, hour=hour)
        p.last_mono = span
        agg._presence[("cam", pid)] = p
    agg._prune_presence(1000.0)
    # 2 tracks, 20s + 40s = 60_000 ms total in the same bucket.
    assert agg._dwell_buf[(sid, "cam", hour)] == [2, 60000]


def test_flow_adjacent() -> None:
    # idx = fy*FLOW_GRID + fx
    a = 5 * FLOW_GRID + 5  # (5,5)
    assert _flow_adjacent(a, 5 * FLOW_GRID + 6) is True  # (6,5) — 1 step
    assert _flow_adjacent(a, 7 * FLOW_GRID + 7) is True  # (7,7) — 2 steps diag
    assert _flow_adjacent(a, 5 * FLOW_GRID + 9) is False  # (9,5) — 4 steps
    assert _flow_adjacent(a, 9 * FLOW_GRID + 5) is False  # (5,9) — 4 steps


def test_hour_bucket_truncates() -> None:
    # 2026-07-06T13:45:30Z → floor to the hour.
    ms = 1783345530000  # arbitrary epoch ms
    dt = _hour_bucket(ms)
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.microsecond == 0
    assert dt.tzinfo is not None
