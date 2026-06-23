"""Route-level test for GET/PUT /api/v1/admin/edge-config — the ONE global edge
config. DB + super-admin auth are overridden and edge_config_repo is monkeypatched,
so this proves the endpoints serialize EdgeConfigAdminView (version + overrides +
effective merged payload) over HTTP — no database required."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.main import create_app
from sentry_backend.repository import edge_config_repo


async def _fake_db() -> AsyncIterator[None]:
    yield None


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_super_admin] = lambda: SimpleNamespace(id=uuid4())
    return TestClient(app)


def test_get_global_edge_config_unset_serves_defaults(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _row(_db: object) -> object | None:
        return None

    monkeypatch.setattr(edge_config_repo, "get_global_row", _row)

    r = client.get("/api/v1/admin/edge-config")
    assert r.status_code == 200
    body = r.json()
    assert body["store_id"] == "global"
    assert body["version"] == 1
    assert body["overrides"] == {}
    # effective merged payload carries the agent defaults
    assert body["effective"]["w_conceal"] == 14.0


def test_put_global_edge_config_returns_merged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = SimpleNamespace(
        value={"version": 3, "overrides": {"w_conceal": 20.0}},
        updated_at_db=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
    )

    async def _set(_db: object, overrides: dict) -> object:
        assert overrides == {"w_conceal": 20.0}
        return row

    async def _commit() -> None: ...
    async def _refresh(_obj: object) -> None: ...

    monkeypatch.setattr(edge_config_repo, "set_global", _set)
    # the route calls db.commit()/db.refresh on the (None) session → patch them off
    client.app.dependency_overrides[get_db] = _patched_db(_commit, _refresh)

    r = client.put("/api/v1/admin/edge-config", json={"w_conceal": 20.0})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 3
    assert body["overrides"] == {"w_conceal": 20.0}
    assert body["effective"]["w_conceal"] == 20.0  # override beats the default


def _patched_db(commit: object, refresh: object):  # noqa: ANN202 — test helper
    async def _db() -> AsyncIterator[object]:
        yield SimpleNamespace(commit=commit, refresh=refresh)

    return _db
