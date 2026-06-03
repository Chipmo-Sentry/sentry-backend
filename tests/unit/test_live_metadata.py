"""REV.1 — typed live-metadata batch parsing (mirrors sentry-ai FrameMetadata)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentry_backend.api.v1.internal import LiveFrame, LiveMetadataBatch, LiveTrack


def _frame(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "camera_id": "store1_cam1",
        "frame_id": 42,
        "ts_ms": 1_700_000_000_000,
        "width": 1920,
        "height": 1080,
        "fps_inference": 8.5,
        "tracks": [
            {
                "person_id": 3,
                "box": [10.0, 20.0, 110.0, 220.0],
                "det_confidence": 0.88,
                "risk_pct": 42.0,
                "color": "yellow",
            }
        ],
    }
    base.update(kw)
    return base


def test_parses_canonical_sentry_ai_frame() -> None:
    batch = LiveMetadataBatch(frames=[_frame()])
    frame = batch.frames[0]
    assert frame.camera_id == "store1_cam1"
    assert frame.tracks[0].person_id == 3
    assert frame.tracks[0].box == (10.0, 20.0, 110.0, 220.0)
    assert frame.tracks[0].color == "yellow"


def test_display_fields_default_when_missing() -> None:
    # Only camera_id + tracks are essential downstream; the rest tolerate drift.
    frame = LiveFrame(camera_id="c1")
    assert frame.width == 0
    assert frame.fps_inference == 0.0
    assert frame.tracks == []


def test_track_risk_defaults_to_zero_green() -> None:
    t = LiveTrack(person_id=1, box=(0, 0, 1, 1))
    assert t.risk_pct == 0.0
    assert t.color == "green"


def test_unknown_keys_ignored_for_forward_compat() -> None:
    frame = LiveFrame(camera_id="c1", future_field="whatever")  # type: ignore[call-arg]
    assert frame.camera_id == "c1"


def test_empty_camera_id_rejected() -> None:
    with pytest.raises(ValidationError):
        LiveFrame(camera_id="")


def test_empty_batch_rejected() -> None:
    with pytest.raises(ValidationError):
        LiveMetadataBatch(frames=[])


def test_bad_color_rejected() -> None:
    with pytest.raises(ValidationError):
        LiveTrack(person_id=1, box=(0, 0, 1, 1), color="purple")  # type: ignore[arg-type]
