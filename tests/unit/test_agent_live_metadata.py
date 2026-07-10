"""POST /api/v1/agent/live-metadata — edge overlay feed.

Proves it publishes each frame to the live WS broker AND the footfall
aggregator (edge cameras count toward /insights analytics), filters frames to
the agent store's OWNED camera paths (no cross-tenant overlay injection), and
does NOT run the threshold handler. DB is a fake returning the store's owned
paths; the broker + aggregator are fakes capturing calls."""

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


class _FakeResult:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[str]:
        return self._rows


class _FakeDB:
    """Returns a fixed set of owned mediamtx_paths for the ownership query."""

    def __init__(self, owned_paths: list[str]) -> None:
        self._owned = owned_paths

    async def execute(self, *_a: object, **_k: object) -> _FakeResult:
        return _FakeResult(self._owned)


# Which paths the agent's store owns (drives the cross-tenant filter).
_OWNED = ["192_168_1_64"]


async def _fake_db() -> AsyncIterator[_FakeDB]:
    yield _FakeDB(_OWNED)


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, key: str, payload: dict) -> None:
        self.published.append((key, payload))


class _FakeAggregator:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def on_frame(self, frame: dict) -> None:
        self.frames.append(frame)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> _FakeBroker:
    b = _FakeBroker()
    monkeypatch.setattr(agents_mod, "get_live_broker", lambda: b)
    return b


@pytest.fixture
def aggregator(monkeypatch: pytest.MonkeyPatch) -> _FakeAggregator:
    a = _FakeAggregator()
    monkeypatch.setattr(agents_mod, "get_footfall_aggregator", lambda: a)
    return a


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_agent] = lambda: SimpleNamespace(
        id=uuid4(), store_id=uuid4(), organization_id=uuid4()
    )
    return TestClient(app)


def _frame(camera_id: str) -> dict:
    return {
        "camera_id": camera_id,
        "frame_id": 1,
        "ts_ms": 1000,
        "width": 1920,
        "height": 1080,
        "tracks": [{"person_id": 7, "box": [0.1, 0.1, 0.3, 0.6], "risk_pct": 42.0}],
    }


def test_publishes_owned_frame_to_broker(
    client: TestClient, broker: _FakeBroker, aggregator: _FakeAggregator
) -> None:
    r = client.post("/api/v1/agent/live-metadata", json={"frames": [_frame("192_168_1_64")]})
    assert r.status_code == 202
    assert r.json() == {"received": 1, "published": 1}
    assert len(broker.published) == 1
    key, payload = broker.published[0]
    assert key == "192_168_1_64"
    assert payload["tracks"][0]["person_id"] == 7
    # Edge frames feed /insights analytics too (heatmap/visits/demographics).
    assert len(aggregator.frames) == 1
    assert aggregator.frames[0]["camera_id"] == "192_168_1_64"


def test_drops_frame_for_unowned_camera(
    client: TestClient, broker: _FakeBroker, aggregator: _FakeAggregator
) -> None:
    # A path the agent's store does NOT own → dropped, never published (no
    # cross-tenant overlay injection) — and never counted in analytics either.
    r = client.post("/api/v1/agent/live-metadata", json={"frames": [_frame("10_0_0_9")]})
    assert r.status_code == 202
    assert r.json() == {"received": 1, "published": 0}
    assert broker.published == []
    assert aggregator.frames == []


def test_empty_frames_rejected(
    client: TestClient, broker: _FakeBroker, aggregator: _FakeAggregator
) -> None:
    # LiveMetadataBatch requires >=1 frame.
    r = client.post("/api/v1/agent/live-metadata", json={"frames": []})
    assert r.status_code == 422
    assert broker.published == []
    assert aggregator.frames == []
