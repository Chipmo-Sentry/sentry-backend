"""Unit tests for the footfall projection (docs/30 F2) — the pure geometry that
turns a normalized camera foot point into a plan grid cell."""

from datetime import UTC, datetime
from uuid import uuid4

from sentry_backend.db.models.analytics_flow import FLOW_GRID
from sentry_backend.db.models.analytics_footfall import GRID_SIZE
from sentry_backend.services.footfall_aggregator import (
    FootfallAggregator,
    _demo_bucket,
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


# The agent stores H as plan → normalized-image. diag(1/sx, 1/sy, 1) maps plan
# (0..sx, 0..sy) → image (0..1); the aggregator inverts it to recover plan from
# an image foot point, so a foot at image (0.5, 0.25) → plan (500, 200).
_PLAN_TO_IMG = [[1 / 1000, 0, 0], [0, 1 / 800, 0], [0, 0, 1]]


def test_identity_homography_maps_norm_to_grid() -> None:
    plan = {"size": [1000, 800], "cameras": [{"camera_id": "cam-1", "homography": _PLAN_TO_IMG}]}
    cell = project_foot_to_cell(plan, "cam-1", 0.5, 0.25)
    assert cell == (int(0.5 * GRID_SIZE), int(0.25 * GRID_SIZE))


def test_homography_clips_out_of_bounds_into_grid() -> None:
    plan = {"size": [1000, 800], "cameras": [{"camera_id": "cam-1", "homography": _PLAN_TO_IMG}]}
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
    plan = {"size": [1000, 800], "cameras": [{"camera_id": "cam-1", "homography": _PLAN_TO_IMG}]}
    result = project_foot_to_plan(plan, "cam-1", 0.5, 0.5)
    assert result is not None
    (px, py), (sx, sy) = result
    assert abs(px - 500) < 1e-6
    assert abs(py - 400) < 1e-6
    assert (sx, sy) == (1000.0, 800.0)


def test_homography_roundtrip_plan_to_image_and_back() -> None:
    """A non-trivial (perspective) plan→image homography: forward-project a known
    plan point to the image, then confirm the aggregator recovers the plan point
    — it must INVERT the stored homography. Guards the stored direction. (No cv2:
    the backend has no opencv, so we build H + the forward transform by hand.)"""
    # A hand-built plan→image homography with genuine perspective (non-zero g,h).
    h = [
        [9.0e-4, 2.0e-4, 5.0e-2],
        [1.0e-4, 1.15e-3, 8.0e-2],
        [1.0e-5, 2.0e-5, 1.0],
    ]

    def forward(px: float, py: float) -> tuple[float, float]:
        w = h[2][0] * px + h[2][1] * py + h[2][2]
        ix = (h[0][0] * px + h[0][1] * py + h[0][2]) / w
        iy = (h[1][0] * px + h[1][1] * py + h[1][2]) / w
        return ix, iy

    plan = {"size": [1000, 800], "cameras": [{"camera_id": "cam-1", "homography": h}]}
    ix, iy = forward(500.0, 400.0)
    assert 0.0 < ix < 1.0 and 0.0 < iy < 1.0  # sanity: lands on-image
    result = project_foot_to_plan(plan, "cam-1", ix, iy)
    assert result is not None
    (px, py), _ = result
    assert abs(px - 500.0) < 0.5
    assert abs(py - 400.0) < 0.5


def test_invert_3x3_identity_and_singular() -> None:
    from sentry_backend.services.footfall_aggregator import _invert_3x3

    ident = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    assert _invert_3x3(ident) == ident
    assert _invert_3x3([[1, 2, 3], [2, 4, 6], [0, 0, 1]]) is None  # singular (row2=2·row1)


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


def test_demo_bucket_normalizes() -> None:
    # No attributes → None (node runs no classifier — contributes nothing).
    assert _demo_bucket({"person_id": 1}) is None
    assert _demo_bucket({"gender": None, "age_band": None}) is None
    # Known vocabulary passes through.
    assert _demo_bucket({"gender": "female", "age_band": "adult"}) == ("female", "adult")
    # Out-of-vocabulary values fold to unknown instead of fanning out rows.
    assert _demo_bucket({"gender": "woman", "age_band": "elderly"}) == ("unknown", "unknown")
    # One-sided attributes still count (the other side is unknown).
    assert _demo_bucket({"gender": "male"}) == ("male", "unknown")
    assert _demo_bucket({"age_band": "child"}) == ("unknown", "child")


def test_demo_seen_dedup_and_prune() -> None:
    # One classified track must count ONCE while continuously visible, and the
    # TTL prune must forget it so a later re-appearance recounts.
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 6, 13, tzinfo=UTC)
    key = (sid, "cam", hour, "female", "adult")

    def hit(now: float) -> None:
        dk = ("cam", 7)
        fresh = dk not in agg._demo_seen
        agg._demo_seen[dk] = now
        if fresh:
            agg._demo_buf[key] = agg._demo_buf.get(key, 0) + 1

    hit(100.0)
    hit(101.0)  # same track next frame — deduped
    assert agg._demo_buf[key] == 1
    agg._gate_last_prune = 0.0
    agg._prune_gate_seen(200.0)  # 99s absent ≥ TTL → forgotten
    assert ("cam", 7) not in agg._demo_seen
    hit(201.0)  # genuine re-appearance recounts
    assert agg._demo_buf[key] == 2


def test_hour_bucket_truncates() -> None:
    # 2026-07-06T13:45:30Z → floor to the hour.
    ms = 1783345530000  # arbitrary epoch ms
    dt = _hour_bucket(ms)
    assert dt.minute == 0
    assert dt.second == 0
    assert dt.microsecond == 0
    assert dt.tzinfo is not None
