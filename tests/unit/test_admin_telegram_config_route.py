"""Route-level test for GET/PUT /api/v1/admin/telegram-config — the platform
Telegram bot token. DB + super-admin auth are overridden and telegram_config_repo
is monkeypatched, so this proves the endpoints mask the secret (only configured +
last-4 hint leave the server) and round-trip a set/clear over HTTP — no DB."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from sentry_backend.deps.auth import require_super_admin
from sentry_backend.deps.db import get_db
from sentry_backend.main import create_app
from sentry_backend.repository import telegram_config_repo


class _FakeDB:
    """Enough of AsyncSession for the endpoint: an async no-op commit (the repo
    calls are monkeypatched, so no real query runs)."""

    async def commit(self) -> None:
        return None


async def _fake_db() -> AsyncIterator[_FakeDB]:
    yield _FakeDB()


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_super_admin] = lambda: SimpleNamespace(id=uuid4())
    return TestClient(app)


def test_get_unset_reports_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _status(_db: object) -> tuple[bool, str | None]:
        return False, None

    monkeypatch.setattr(telegram_config_repo, "get_status", _status)

    r = client.get("/api/v1/admin/telegram-config")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "token_hint": None}


def test_get_configured_returns_hint_not_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _status(_db: object) -> tuple[bool, str | None]:
        return True, "…cdef"

    monkeypatch.setattr(telegram_config_repo, "get_status", _status)

    r = client.get("/api/v1/admin/telegram-config")
    body = r.json()
    assert body["configured"] is True
    assert body["token_hint"] == "…cdef"
    # The full token must never appear anywhere in the response.
    assert "bot_token" not in body


def test_put_sets_token_and_returns_masked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: dict[str, str | None] = {}

    async def _set(_db: object, token: str | None) -> None:
        saved["token"] = token

    async def _status(_db: object) -> tuple[bool, str | None]:
        return (bool(saved.get("token")), "…AAAA" if saved.get("token") else None)

    monkeypatch.setattr(telegram_config_repo, "set_token", _set)
    monkeypatch.setattr(telegram_config_repo, "get_status", _status)

    r = client.put("/api/v1/admin/telegram-config", json={"bot_token": "123456:AAAA"})
    assert r.status_code == 200
    assert saved["token"] == "123456:AAAA"
    assert r.json() == {"configured": True, "token_hint": "…AAAA"}


def test_put_empty_clears_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, str | None] = {"token": "old"}

    async def _set(_db: object, token: str | None) -> None:
        saved["token"] = token  # empty string → None via the endpoint's strip()

    async def _status(_db: object) -> tuple[bool, str | None]:
        return (bool(saved.get("token")), None)

    monkeypatch.setattr(telegram_config_repo, "set_token", _set)
    monkeypatch.setattr(telegram_config_repo, "get_status", _status)

    r = client.put("/api/v1/admin/telegram-config", json={"bot_token": "  "})
    assert r.status_code == 200
    assert saved["token"] is None
    assert r.json() == {"configured": False, "token_hint": None}
