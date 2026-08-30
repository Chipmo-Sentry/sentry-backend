"""Route-level test for GET /api/v1/admin/stores — the superadmin store picker.

The DB + super-admin auth deps are overridden and the store_repo lookup is
monkeypatched, so this proves the endpoint serializes StoreAdminRow (id, name,
org name, camera count) over HTTP — no database required."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.main import create_app
from sentry_backend.repository import store_repo


async def _fake_db() -> AsyncIterator[None]:
    yield None  # the monkeypatched repo lookup ignores the session


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_super_admin] = lambda: SimpleNamespace(id=uuid4())
    return TestClient(app)


def test_admin_stores_returns_rows(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sid, oid = uuid4(), uuid4()
    store = SimpleNamespace(
        id=sid,
        name="Дэлгүүр A",
        organization_id=oid,
        agent_stream_push_url="rtsp://example:8554",
        agent_tunnel_hostname=None,
        agent_tunnel_token=None,
        staff_badge_color=None,
    )

    async def _rows(_db: object) -> list[tuple[object, str, int]]:
        return [(store, "Org A", 3)]

    monkeypatch.setattr(store_repo, "list_all_with_org_and_camera_count", _rows)

    r = client.get("/api/v1/admin/stores")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    assert row["id"] == str(sid)
    assert row["name"] == "Дэлгүүр A"
    assert row["organization_id"] == str(oid)
    assert row["organization_name"] == "Org A"
    assert row["camera_count"] == 3
    assert row["agent_stream_push_url"] == "rtsp://example:8554"


def test_admin_stores_empty(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rows(_db: object) -> list[object]:
        return []

    monkeypatch.setattr(store_repo, "list_all_with_org_and_camera_count", _rows)

    r = client.get("/api/v1/admin/stores")
    assert r.status_code == 200
    assert r.json() == []
