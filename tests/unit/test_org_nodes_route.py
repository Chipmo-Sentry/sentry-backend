"""Route-level wiring for GET /api/v1/nodes — no DB, no camera.

Exercises the real FastAPI route (routing + OrgNodePublic response_model
serialization + the org-projection wiring) with the DB/auth deps overridden and
the two repo calls monkeypatched. Complements test_org_nodes.py (which unit-tests
the pure build_org_node) by proving the endpoint itself returns the projected,
tenant-filtered payload over HTTP."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import get_current_organization_id
from sentry_backend.main import create_app
from sentry_backend.repository import ai_node_repo, camera_repo

NOW = datetime.now(UTC)


@dataclass
class FakeNode:
    telemetry: str | None
    last_seen_at: datetime | None = NOW
    name: str | None = "Predator-1"
    version: str | None = "0.5.0"
    gpu: str | None = "RTX 4090"
    provider: str = "qwen3-vl-4b"
    breach_mode: str = "node_push"
    id: UUID = field(default_factory=uuid4)


def _telemetry(cams: list[tuple[str, float, str]], **extra: object) -> str:
    body: dict[str, object] = {
        "cameras": [{"camera_id": c, "fps": f, "status": s} for c, f, s in cams]
    }
    body.update(extra)
    return json.dumps(body)


async def _fake_db() -> AsyncIterator[None]:
    yield None  # the monkeypatched repos ignore the session


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_organization_id] = lambda: uuid4()
    # No lifespan (DB-touching startup) — call the route directly, not via `with`.
    return TestClient(app)


def _set_cameras(monkeypatch: pytest.MonkeyPatch, paths: list[str | None]) -> None:
    async def _list(_db: object, _org: object, store_id: object = None) -> list[object]:
        return [SimpleNamespace(mediamtx_path=p) for p in paths]

    monkeypatch.setattr(camera_repo, "list_cameras_for_org", _list)


def _set_nodes(monkeypatch: pytest.MonkeyPatch, nodes: list[FakeNode]) -> None:
    async def _list(_db: object) -> list[FakeNode]:
        return nodes

    monkeypatch.setattr(ai_node_repo, "list_nodes", _list)


def test_returns_empty_when_org_has_no_cameras(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cameras(monkeypatch, [])
    _set_nodes(monkeypatch, [FakeNode(telemetry=_telemetry([("cam-a", 4.8, "ok")]))])
    r = client.get("/api/v1/nodes")
    assert r.status_code == 200
    assert r.json() == []


def test_projects_only_callers_cameras_over_http(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Org owns cam-a; node ALSO serves another tenant's cam-secret.
    _set_cameras(monkeypatch, ["cam-a"])
    _set_nodes(
        monkeypatch,
        [
            FakeNode(
                telemetry=_telemetry(
                    [("cam-a", 4.8, "ok"), ("cam-secret", 5.0, "ok")],
                    fps_inference=9.6,
                    gpu_pct=99,
                    provider_effective="qwen3-vl-4b",
                    provider_ready=True,
                )
            )
        ],
    )
    r = client.get("/api/v1/nodes")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    node = body[0]
    cam_ids = [c["camera_id"] for c in node["cameras"]]
    assert cam_ids == ["cam-a"]
    # The other tenant's camera id must not leak ANYWHERE in the response.
    assert "cam-secret" not in r.text
    assert "telemetry" not in node
    # Whole-node gauges + central-control fields project through.
    assert node["fps_inference"] == 9.6
    assert node["gpu_pct"] == 99
    assert node["provider_effective"] == "qwen3-vl-4b"
    assert node["is_online"] is True


def test_hides_node_serving_no_org_cameras(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_cameras(monkeypatch, ["cam-a"])
    _set_nodes(monkeypatch, [FakeNode(telemetry=_telemetry([("cam-other", 4.8, "ok")]))])
    r = client.get("/api/v1/nodes")
    assert r.status_code == 200
    assert r.json() == []
