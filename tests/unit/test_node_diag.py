"""Node diagnostics push + org-scoped read. No DB.

The node POSTs a free-form diag (ai_node JWT); the org reads it back gated by the
same derived ownership as the node list (404 for an unowned node)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.ai_node_auth import get_current_ai_node
from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.main import create_app
from sentry_backend.repository import ai_node_repo, camera_repo


@dataclass
class FakeNode:
    telemetry: str | None
    id: UUID = field(default_factory=uuid4)


def _telemetry(cams: list[str]) -> str:
    return json.dumps({"cameras": [{"camera_id": c, "fps": 1.0, "status": "ok"} for c in cams]})


async def _fake_db() -> AsyncIterator[None]:
    yield None


def _set_cameras(monkeypatch: pytest.MonkeyPatch, paths: list[str]) -> None:
    async def _list(_db: object, _org: object, store_id: object = None) -> list[object]:
        return [SimpleNamespace(mediamtx_path=p) for p in paths]

    monkeypatch.setattr(camera_repo, "list_cameras_for_org", _list)


def _set_node(monkeypatch: pytest.MonkeyPatch, node: FakeNode | None) -> None:
    async def _get(_db: object, _id: object) -> FakeNode | None:
        return node

    monkeypatch.setattr(ai_node_repo, "get_node", _get)


def test_push_then_read_for_owned_node(monkeypatch: pytest.MonkeyPatch) -> None:
    node = FakeNode(telemetry=_telemetry(["cam-a"]))
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_ai_node] = lambda: node
    app.dependency_overrides[get_current_organization_id] = lambda: uuid4()
    client = TestClient(app)

    diag = {"vlm": {"parse_fail_pct": 87, "verdicts": [{"category": "other", "raw": "oops"}]}}
    r = client.post("/api/v1/internal/node-diag", json=diag)
    assert r.status_code == 204

    _set_cameras(monkeypatch, ["cam-a"])
    _set_node(monkeypatch, node)
    r = client.get(f"/api/v1/nodes/{node.id}/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["diag"]["vlm"]["parse_fail_pct"] == 87
    assert isinstance(body["age_sec"], int)


def test_read_404_for_unowned_node(monkeypatch: pytest.MonkeyPatch) -> None:
    node = FakeNode(telemetry=_telemetry(["cam-other"]))
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_organization_id] = lambda: uuid4()
    client = TestClient(app)
    _set_cameras(monkeypatch, ["cam-a"])
    _set_node(monkeypatch, node)
    r = client.get(f"/api/v1/nodes/{node.id}/diag")
    assert r.status_code == 404


def test_read_available_false_when_never_pushed(monkeypatch: pytest.MonkeyPatch) -> None:
    node = FakeNode(telemetry=_telemetry(["cam-a"]))
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_organization_id] = lambda: uuid4()
    client = TestClient(app)
    _set_cameras(monkeypatch, ["cam-a"])
    _set_node(monkeypatch, node)
    r = client.get(f"/api/v1/nodes/{node.id}/diag")
    assert r.status_code == 200
    assert r.json() == {"available": False, "age_sec": None, "diag": None}
