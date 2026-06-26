"""POST /api/v1/agent/live-metadata — edge overlay feed.

Proves it publishes each frame to the live WS broker and does NOT run the
threshold handler (so the edge alone decides alerts). DB + agent auth overridden;
the broker is a fake capturing publishes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import sentry_backend.api.v1.agents as agents_mod
from sentry_backend.deps.agent_auth import get_current_agent
from sentry_backend.deps.db import get_db
from sentry_backend.main import create_app


async def _fake_db() -> AsyncIterator[None]:
    yield None


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> _FakeBroker:
    b = _FakeBroker()
    monkeypatch.setattr(agents_mod, "get_live_broker", lambda: b)
    return b


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_agent] = lambda: SimpleNamespace(
        id=uuid4(), store_id=uuid4(), organization_id=uuid4()
    )
    return TestClient(app)


def test_publishes_each_frame_to_broker(client: TestClient, broker: _FakeBroker) -> None:
    body = {
        "frames": [
            {
                "camera_id": "192_168_1_64",
                "frame_id": 1,
                "ts_ms": 1000,
                "width": 1920,
                "height": 1080,
                "tracks": [{"person_id": 7, "box": [0.1, 0.1, 0.3, 0.6], "risk_pct": 42.0}],
            }
        ]
    }
    r = client.post("/api/v1/agent/live-metadata", json=body)
    assert r.status_code == 202
    assert r.json() == {"received": 1, "published": 1}
    assert len(broker.published) == 1
    key, payload = broker.published[0]
    assert key == "192_168_1_64"
    assert payload["tracks"][0]["person_id"] == 7


def test_empty_frames_rejected(client: TestClient, broker: _FakeBroker) -> None:
    # LiveMetadataBatch requires >=1 frame.
    r = client.post("/api/v1/agent/live-metadata", json={"frames": []})
    assert r.status_code == 422
    assert broker.published == []
