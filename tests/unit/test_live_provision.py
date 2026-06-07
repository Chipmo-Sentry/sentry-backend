"""live_provision orchestration — enabled provisions both, disabled tears down."""

from __future__ import annotations

import pytest

from sentry_backend.services import live_ai_client, live_provision


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    rec: dict[str, list] = {"add": [], "del": [], "start": [], "stop": []}

    async def add_path(name: str, rtsp: str) -> bool:
        rec["add"].append((name, rtsp))
        return True

    async def delete_path(name: str) -> bool:
        rec["del"].append(name)
        return True

    async def start_worker(name: str, store_id: str | None = None) -> bool:
        rec["start"].append(name)
        return True

    async def stop_worker(name: str) -> bool:
        rec["stop"].append(name)
        return True

    monkeypatch.setattr(live_provision.mediamtx_client, "add_path", add_path)
    monkeypatch.setattr(live_provision.mediamtx_client, "delete_path", delete_path)
    monkeypatch.setattr(live_provision.live_ai_client, "start_worker", start_worker)
    monkeypatch.setattr(live_provision.live_ai_client, "stop_worker", stop_worker)
    return rec


async def test_provision_enabled_adds_path_and_starts_ai(calls: dict[str, list]) -> None:
    await live_provision.provision("cam1", "rtsp://cam/1", enabled=True)
    assert calls["add"] == [("cam1", "rtsp://cam/1")]
    assert calls["start"] == ["cam1"]
    assert calls["del"] == [] and calls["stop"] == []


async def test_provision_disabled_tears_down(calls: dict[str, list]) -> None:
    await live_provision.provision("cam1", "rtsp://cam/1", enabled=False)
    assert calls["stop"] == ["cam1"]
    assert calls["del"] == ["cam1"]
    assert calls["add"] == [] and calls["start"] == []


async def test_deprovision_stops_ai_then_deletes_path(calls: dict[str, list]) -> None:
    await live_provision.deprovision("cam1")
    assert calls["stop"] == ["cam1"]
    assert calls["del"] == ["cam1"]


def test_mediamtx_stream_url_builds_from_base(monkeypatch: pytest.MonkeyPatch) -> None:
    from sentry_backend.settings import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setenv("MEDIAMTX_RTSP_URL", "rtsp://mtx:8554")
    assert live_ai_client.mediamtx_stream_url("cam1") == "rtsp://mtx:8554/cam1"
    get_settings.cache_clear()  # type: ignore[attr-defined]
