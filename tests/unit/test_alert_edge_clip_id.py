"""Edge-first traceability: an alert's `edge_clip_id` (the agent-pc edge clip id)
survives the model → AlertPublic round-trip so the frontend can show it as an
"ID" column matching the agent-pc «Сэжигтэй» row. No DB — pure schema mapping."""

from __future__ import annotations

from sentry_backend.db.models.alert import (
    Alert,
    AlertCategory,
    AlertLevel,
    AlertTrigger,
)
from sentry_backend.schemas.alert import AlertPublic


def _alert(**kw: object) -> Alert:
    from datetime import UTC, datetime
    from uuid import uuid4

    defaults: dict[str, object] = {
        "id": uuid4(),
        "clip_id": uuid4(),
        "created_at": datetime(2026, 6, 26, tzinfo=UTC),
        "organization_id": uuid4(),
        "store_id": None,
        "camera_id": None,
        "category": AlertCategory.pocket_conceal,
        "confidence": 0.91,
        "reasoning": "Бараа халаасандаа хийсэн",
        "model_name": "qwen3-vl",
        "alert_level": AlertLevel.review,
        "inference_latency_ms": 300,
    }
    defaults.update(kw)
    return Alert(**defaults)


def test_edge_clip_id_exposed_for_edge_upload() -> None:
    alert = _alert(
        triggered_by=AlertTrigger.edge_pc_upload,
        edge_clip_id="192_168_1_64_1719393023",
    )
    pub = AlertPublic.model_validate(alert)
    assert pub.edge_clip_id == "192_168_1_64_1719393023"


def test_edge_clip_id_none_for_cloud_alert() -> None:
    # Cloud/live/manual alerts never set edge_clip_id → the UI shows "—".
    alert = _alert(triggered_by=AlertTrigger.live_threshold)
    pub = AlertPublic.model_validate(alert)
    assert pub.edge_clip_id is None
