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


# === REV.2 — v2 behavior-engine context fields survive validation ===


def test_rev2_episode_fields_pass_through() -> None:
    """sequences/behaviors/etc. were previously stripped by extra="ignore" —
    they must now survive into the dict published to browsers + threshold."""
    track = {
        "person_id": 3,
        "box": [10.0, 20.0, 110.0, 220.0],
        "det_confidence": 0.88,
        "risk_pct": 72.0,
        "color": "red",
        "level": "HIGH",
        "state": "CONCEALMENT",
        "sequences": ["seq_pickup_bag"],
        "behaviors": ["looking_around", "item_pickup", "bag_interaction"],
        "behavior_scores": {"looking_around": 4.0, "item_pickup": 10.0},
        "reasons": ["Орчноо харах", "cell phone авах"],
        "episode_started_ms": 1_700_000_000_000,
        "store_person_id": 12,
        "store_risk_pct": 80.5,
    }
    frame = LiveFrame.model_validate(_frame(tracks=[track]))
    dumped = frame.model_dump(mode="json")["tracks"][0]
    assert dumped["sequences"] == ["seq_pickup_bag"]
    assert dumped["behaviors"] == ["looking_around", "item_pickup", "bag_interaction"]
    assert dumped["behavior_scores"] == {"looking_around": 4.0, "item_pickup": 10.0}
    assert dumped["reasons"] == ["Орчноо харах", "cell phone авах"]
    assert dumped["episode_started_ms"] == 1_700_000_000_000
    assert dumped["level"] == "HIGH"
    assert dumped["state"] == "CONCEALMENT"
    assert dumped["store_person_id"] == 12
    assert dumped["store_risk_pct"] == 80.5


def test_rev2_fields_default_for_old_ai_nodes() -> None:
    """An older AI node that doesn't send the new fields still validates."""
    frame = LiveFrame.model_validate(_frame())
    dumped = frame.model_dump(mode="json")["tracks"][0]
    assert dumped["behaviors"] == []
    assert dumped["behavior_scores"] == {}
    assert dumped["reasons"] == []
    assert dumped["episode_started_ms"] is None
    assert dumped["sequences"] == []


# === held-item overlay: items must survive validation → browser fanout ===


def test_items_pass_through_to_fanout() -> None:
    """Held-item box + Mongolian name must reach the browser; extra="ignore"
    would strip them without the LiveItem mirror."""
    items = [
        {
            "box": [305.0, 300.0, 360.0, 330.0],
            "label": "bottle",
            "label_mn": "лонх",
            "confidence": 0.81,
            "held": True,
        }
    ]
    frame = LiveFrame.model_validate(_frame(items=items))
    dumped = frame.model_dump(mode="json")["items"][0]
    assert dumped["box"] == [305.0, 300.0, 360.0, 330.0]
    assert dumped["label_mn"] == "лонх"
    assert dumped["held"] is True


def test_items_default_empty_for_old_ai_nodes() -> None:
    frame = LiveFrame.model_validate(_frame())
    assert frame.model_dump(mode="json")["items"] == []
