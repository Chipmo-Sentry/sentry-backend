"""GET /api/v1/ingest/paths — org-scoped MediaMTX path state. No DB, no MediaMTX.

Mocks camera_repo + mediamtx_client.list_paths and asserts the endpoint projects
runtime path state to the caller-org's cameras only, and reports availability
honestly when the MediaMTX API can't be reached."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.main import create_app
from sentry_backend.repository import camera_repo
from sentry_backend.services import mediamtx_client


async def _fake_db() -> AsyncIterator[None]:
    yield None


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_organization_id] = lambda: uuid4()
    return TestClient(app)


def _set_cameras(monkeypatch: pytest.MonkeyPatch, paths: list[tuple[str, str]]) -> None:
    async def _list(_db: object, _org: object, store_id: object = None) -> list[object]:
        return [SimpleNamespace(mediamtx_path=p, name=n) for p, n in paths]

    monkeypatch.setattr(camera_repo, "list_cameras_for_org", _list)


def _set_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, dict] | None) -> None:
    async def _list() -> dict[str, dict] | None:
        return state

    monkeypatch.setattr(mediamtx_client, "list_paths", _list)


def test_projects_only_org_paths(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cameras(monkeypatch, [("cam-a", "Орц")])
    _set_state(
        monkeypatch,
        {
            "cam-a": {"ready": True, "readers": 2, "has_source": True},
            "cam-other": {"ready": True, "readers": 1, "has_source": True},  # other tenant
        },
    )
    r = client.get("/api/v1/ingest/paths")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert [p["path"] for p in body["paths"]] == ["cam-a"]
    assert body["paths"][0]["ready"] is True
    assert body["paths"][0]["name"] == "Орц"
    assert "cam-other" not in r.text


def test_unavailable_when_mediamtx_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cameras(monkeypatch, [("cam-a", "Орц")])
    _set_state(monkeypatch, None)  # API disabled / unreachable
    r = client.get("/api/v1/ingest/paths")
    assert r.status_code == 200
    assert r.json() == {"available": False, "paths": []}


def test_path_not_yet_publishing_is_not_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cameras(monkeypatch, [("cam-a", "Орц"), ("cam-b", "Гарц")])
    _set_state(
        monkeypatch,
        {
            "cam-a": {"ready": True, "readers": 1, "has_source": True},
            # cam-b configured but no publisher yet → not ready
            "cam-b": {"ready": False, "readers": 0, "has_source": False},
        },
    )
    r = client.get("/api/v1/ingest/paths")
    body = r.json()
    ready = {p["path"]: p["ready"] for p in body["paths"]}
    assert ready == {"cam-a": True, "cam-b": False}


def test_empty_when_org_has_no_cameras(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cameras(monkeypatch, [])
    _set_state(monkeypatch, {"cam-x": {"ready": True, "readers": 1}})
    r = client.get("/api/v1/ingest/paths")
    assert r.json() == {"available": True, "paths": []}
