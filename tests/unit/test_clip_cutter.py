"""clip_cutter timestamp parsing + segment-window selection.

Regression guard: the MediaMTX filename format "YYYY-MM-DD_HH-MM-SS-µs.mp4"
uses `_` to separate date and time, then `-` within each half. An early
parser split the whole string on `-` and mis-parsed every segment, causing
"no segments overlap window" on every threshold breach. These tests lock the
correct behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sentry_backend.services import clip_cutter


def test_parse_segment_ts_with_micros() -> None:
    ts = clip_cutter._parse_segment_ts("2026-05-28_14-58-34-265139.mp4")
    assert ts == datetime(2026, 5, 28, 14, 58, 34, 265139)


def test_parse_segment_ts_without_micros() -> None:
    ts = clip_cutter._parse_segment_ts("2026-05-28_14-58-34.mp4")
    assert ts == datetime(2026, 5, 28, 14, 58, 34)


def test_parse_segment_ts_rejects_garbage() -> None:
    assert clip_cutter._parse_segment_ts("not-a-segment.mp4") is None
    assert clip_cutter._parse_segment_ts("2026-05-28.mp4") is None


def test_parse_segment_ts_ordering() -> None:
    """Parsed timestamps must sort chronologically (the original bug broke this)."""
    names = [
        "2026-05-28_14-58-34-100000.mp4",
        "2026-05-28_14-59-34-100000.mp4",
        "2026-05-28_15-00-07-787907.mp4",
    ]
    parsed = [clip_cutter._parse_segment_ts(n) for n in names]
    assert all(p is not None for p in parsed)
    assert parsed == sorted(parsed)  # type: ignore[type-var]


def test_find_segments_in_window(tmp_path: Path) -> None:
    """Segments overlapping [start, end] are returned; others excluded."""
    cam_dir = tmp_path / "cam1"
    cam_dir.mkdir()
    # 60-sec segments at :00, :01, :02
    names = [
        "2026-05-28_14-00-00-000000.mp4",
        "2026-05-28_14-01-00-000000.mp4",
        "2026-05-28_14-02-00-000000.mp4",
    ]
    for n in names:
        (cam_dir / n).write_bytes(b"x")

    # Window 14:01:10 .. 14:01:20 → only the 14:01 segment (next starts 14:02)
    start = datetime(2026, 5, 28, 14, 1, 10)
    end = start + timedelta(seconds=10)
    segs = clip_cutter._find_segments_in_window(cam_dir, start, end)
    assert len(segs) == 1
    assert segs[0].name == "2026-05-28_14-01-00-000000.mp4"


def test_find_segments_spanning_boundary(tmp_path: Path) -> None:
    """A window crossing a segment boundary returns both segments."""
    cam_dir = tmp_path / "cam2"
    cam_dir.mkdir()
    for n in [
        "2026-05-28_14-00-00-000000.mp4",
        "2026-05-28_14-01-00-000000.mp4",
    ]:
        (cam_dir / n).write_bytes(b"x")

    # 14:00:55 .. 14:01:05 spans the boundary
    start = datetime(2026, 5, 28, 14, 0, 55)
    end = start + timedelta(seconds=10)
    segs = clip_cutter._find_segments_in_window(cam_dir, start, end)
    assert len(segs) == 2


def test_find_segments_skips_cut_outputs(tmp_path: Path) -> None:
    """Our own `live_*` cut files must not be treated as source segments."""
    cam_dir = tmp_path / "cam3"
    cam_dir.mkdir()
    (cam_dir / "2026-05-28_14-00-00-000000.mp4").write_bytes(b"x")
    (cam_dir / "live_20260528T140000Z_cam3_abc.mp4").write_bytes(b"x")

    start = datetime(2026, 5, 28, 14, 0, 10)
    end = start + timedelta(seconds=5)
    segs = clip_cutter._find_segments_in_window(cam_dir, start, end)
    assert all(not s.name.startswith("live_") for s in segs)


def test_find_segments_missing_dir(tmp_path: Path) -> None:
    segs = clip_cutter._find_segments_in_window(
        tmp_path / "nonexistent", datetime.now(), datetime.now(),
    )
    assert segs == []
