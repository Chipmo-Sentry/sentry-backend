"""Фаз 0 (ADR-0030): the skeleton trajectory must ride from the node-push alert
through to the verified_case, so (pose + staff verdict) becomes a training row."""

from __future__ import annotations

from datetime import UTC, datetime

from sentry_backend.db.models.alert import AlertCategory
from sentry_backend.repository import rag_case_repo
from sentry_backend.schemas.alert import LiveAlertCreate

_SEQ = [
    {"frame_idx": 1, "ts_ms": 1000, "keypoints": [[1.0, 2.0, 0.9]], "box": [0, 0, 10, 20]},
    {"frame_idx": 2, "ts_ms": 1200, "keypoints": [[1.1, 2.1, 0.9]], "box": [0, 0, 10, 20]},
]


def _live_alert(**kw: object) -> LiveAlertCreate:
    base: dict[str, object] = {
        "camera_id": "cam1",
        "category": AlertCategory.pocket_conceal,
        "confidence": 0.9,
        "reasoning": "халаасандаа нуув",
        "model_name": "skeleton",
        "inference_latency_ms": 12,
        "clip_b64": "AAAA",
        "file_size_bytes": 100,
        "duration_sec_clip": 3.0,
        "captured_at": datetime.now(UTC),
    }
    base.update(kw)
    return LiveAlertCreate(**base)  # type: ignore[arg-type]


def test_live_alert_create_accepts_pose_sequence() -> None:
    m = _live_alert(pose_sequence=_SEQ)
    assert m.pose_sequence == _SEQ


def test_live_alert_create_pose_sequence_optional() -> None:
    assert _live_alert().pose_sequence is None  # older nodes don't send it


async def test_add_case_persists_pose_sequence() -> None:
    """rag_case_repo.add_case stores the pose on the VerifiedCase (the labelled
    training row), alongside the verdict."""
    captured: dict[str, object] = {}

    class _FakeDb:
        def add(self, obj: object) -> None:
            captured["case"] = obj

        async def flush(self) -> None:
            return None

    case = await rag_case_repo.add_case(
        _FakeDb(),  # type: ignore[arg-type]
        store_id=None,
        verdict="true_positive",
        category="pocket_conceal",
        description="халаасандаа нуув",
        embedding=[0.1, 0.2],
        pose_sequence=_SEQ,
    )
    assert case.pose_sequence == _SEQ
    assert case.verdict == "true_positive"
    assert captured["case"] is case


async def test_add_case_pose_sequence_optional() -> None:
    class _FakeDb:
        def add(self, obj: object) -> None:
            return None

        async def flush(self) -> None:
            return None

    case = await rag_case_repo.add_case(
        _FakeDb(),  # type: ignore[arg-type]
        store_id=None,
        verdict="false_positive",
        category=None,
        description="x",
        embedding=[0.1],
    )
    assert case.pose_sequence is None
