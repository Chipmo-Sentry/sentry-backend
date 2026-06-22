"""Route-level test for GET /api/v1/admin/alerts — the superadmin pipeline trace.

The DB + super-admin auth deps are overridden and the repo + feedback lookups are
monkeypatched, so this proves the endpoint wires AdminAlertRow serialization
(org/store/camera display names, latest feedback verdict, tz-aware created_at,
triggered behaviours) over HTTP — no database required."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.db.models.alert import AlertCategory, AlertLevel, AlertTrigger
from sentry_backend.db.models.feedback import FeedbackVerdict
from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.main import create_app
from sentry_backend.repository import alert_repo, feedback_repo


@dataclass
class FakeAlert:
    id: UUID = field(default_factory=uuid4)
    clip_id: UUID = field(default_factory=uuid4)
    organization_id: UUID = field(default_factory=uuid4)
    store_id: UUID | None = None
    camera_id: UUID | None = field(default_factory=uuid4)
    category: AlertCategory = AlertCategory.pocket_conceal
    actions: list[str] | None = None
    confidence: float = 0.91
    reasoning: str = "pocketed an item"
    model_name: str = "qwen3-vl"
    alert_level: AlertLevel = AlertLevel.review
    inference_latency_ms: int = 315
    created_at: datetime = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
    triggered_by: AlertTrigger = AlertTrigger.live_threshold
    person_id: int | None = 7
    peak_risk_pct: float | None = 86.0
    triggered_behaviors: list[str] | None = field(
        default_factory=lambda: ["pocket_conceal", "looking_around"]
    )
    triggered_sequences: list[str] | None = None
    triggered_behavior_detail: list[dict] | None = None
    feedback_verdict: FeedbackVerdict | None = None


async def _fake_db() -> AsyncIterator[None]:
    yield None  # the monkeypatched repo + feedback lookups ignore the session


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_super_admin] = lambda: SimpleNamespace(id=uuid4())
    # No lifespan (DB-touching startup) — call the route directly, not via `with`.
    return TestClient(app)


def test_admin_alerts_returns_enriched_trace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert = FakeAlert()

    edge = [{"key": "wrist_to_torso", "offset_sec": 1.2, "score": 3.0}]

    async def _rows(_db: object, **_kw: object) -> list[tuple[object, ...]]:
        return [(alert, "Org A", "Store A", "Cam-1", edge, 72.0)]

    async def _verdicts(_db: object, _ids: object) -> dict[UUID, FeedbackVerdict]:
        return {alert.id: FeedbackVerdict.false_positive}

    monkeypatch.setattr(alert_repo, "list_recent_admin", _rows)
    monkeypatch.setattr(feedback_repo, "latest_verdicts_for_alerts", _verdicts)

    r = client.get("/api/v1/admin/alerts")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    assert row["organization_name"] == "Org A"
    assert row["store_name"] == "Store A"
    assert row["camera_name"] == "Cam-1"
    assert row["alert_level"] == "review"
    assert row["feedback_verdict"] == "false_positive"
    assert row["triggered_behaviors"] == ["pocket_conceal", "looking_around"]
    assert row["created_at"].startswith("2026-06-20T12:00:00")
    assert row["edge_behavior_detail"] == edge
    assert row["edge_risk_pct"] == 72.0


def test_admin_alerts_empty(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rows(_db: object, **_kw: object) -> list[object]:
        return []

    async def _verdicts(_db: object, _ids: object) -> dict[UUID, FeedbackVerdict]:
        return {}

    monkeypatch.setattr(alert_repo, "list_recent_admin", _rows)
    monkeypatch.setattr(feedback_repo, "latest_verdicts_for_alerts", _verdicts)

    r = client.get("/api/v1/admin/alerts")
    assert r.status_code == 200
    assert r.json() == []
