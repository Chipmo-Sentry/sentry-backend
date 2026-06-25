"""Route-level tests for the agent push-status surface (cloud pipeline view).

Covers:
  • GET  /api/v1/nodes/agent-push   — projects org agents' stored push_status to
    per-camera rows with online derivation.
  • PATCH /api/v1/admin/stores/{id} — superadmin repoints a store's push target.

DB + auth deps are overridden and the repo calls monkeypatched, so these prove
the HTTP layer without a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.main import create_app
from sentry_backend.repository import agent_repo, camera_repo, org_repo, store_repo


async def _fake_db() -> AsyncIterator[None]:
    yield None


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_super_admin] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_current_organization_id] = lambda: uuid4()
    return TestClient(app)


def test_agent_push_projects_paths(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(
        id=uuid4(),
        name="Store PC",
        last_seen_at=datetime.now(UTC),  # fresh → online
        push_status=[
            {"path": "192_168_1_64_2", "running": False, "restarts": 3, "last_error": "refused"},
        ],
    )

    async def _agents(_db: object, _org: object) -> list[object]:
        return [agent]

    monkeypatch.setattr(agent_repo, "list_active_agents_for_org", _agents)

    r = client.get("/api/v1/nodes/agent-push")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert len(paths) == 1
    p = paths[0]
    assert p["path"] == "192_168_1_64_2"
    assert p["running"] is False
    assert p["last_error"] == "refused"
    assert p["agent_online"] is True


def test_agent_push_marks_stale_agent_offline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = SimpleNamespace(
        id=uuid4(),
        name="Store PC",
        last_seen_at=datetime.now(UTC) - timedelta(minutes=10),  # stale → offline
        push_status=[{"path": "cam_a", "running": True, "restarts": 0, "last_error": None}],
    )

    async def _agents(_db: object, _org: object) -> list[object]:
        return [agent]

    monkeypatch.setattr(agent_repo, "list_active_agents_for_org", _agents)

    r = client.get("/api/v1/nodes/agent-push")
    assert r.status_code == 200
    assert r.json()["paths"][0]["agent_online"] is False


def test_admin_patch_store_push_url(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid, oid = uuid4(), uuid4()
    store = SimpleNamespace(
        id=sid, name="Дэлгүүр A", organization_id=oid, agent_stream_push_url=None
    )

    async def _get(_db: object, _id: object) -> object:
        return store

    async def _update(_db: object, s: object, *, agent_stream_push_url: str | None) -> object:
        # Mirror the repo's empty-string-clears semantics.
        s.agent_stream_push_url = agent_stream_push_url or None
        return s

    async def _count(_db: object, _sid: object) -> int:
        return 2

    async def _org(_db: object, _oid: object) -> object:
        return SimpleNamespace(name="Org A")

    monkeypatch.setattr(store_repo, "get_store_any_org", _get)
    monkeypatch.setattr(store_repo, "update_store", _update)
    monkeypatch.setattr(camera_repo, "count_cameras_for_store", _count)
    monkeypatch.setattr(org_repo, "get_org", _org)

    r = client.patch(
        f"/api/v1/admin/stores/{sid}",
        json={"agent_stream_push_url": "rtsp://1.2.3.4:8554"},
    )
    assert r.status_code == 200
    assert r.json()["agent_stream_push_url"] == "rtsp://1.2.3.4:8554"


def test_admin_patch_store_not_found(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get(_db: object, _id: object) -> None:
        return None

    monkeypatch.setattr(store_repo, "get_store_any_org", _get)

    r = client.patch(
        f"/api/v1/admin/stores/{uuid4()}",
        json={"agent_stream_push_url": "rtsp://x"},
    )
    assert r.status_code == 404
