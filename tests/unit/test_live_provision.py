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

    async def start_worker(
        name: str,
        store_id: str | None = None,
        risk_threshold: float | None = None,
        zones: list[dict[str, object]] | None = None,
    ) -> bool:
        rec["start"].append({"name": name, "store_id": store_id, "zones": zones})
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
    assert [c["name"] for c in calls["start"]] == ["cam1"]
    assert calls["del"] == [] and calls["stop"] == []


async def test_provision_forwards_zones_to_worker(calls: dict[str, list]) -> None:
    zones = [{"type": "exit", "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}]
    await live_provision.provision("cam1", "rtsp://cam/1", enabled=True, zones=zones)
    assert calls["start"][0]["zones"] == zones


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


class _FakeResp:
    status_code = 202
    text = ""


class _CaptureClient:
    """Captures the JSON body of the single POST start_worker makes."""

    captured: dict[str, object] | None = None

    def __init__(self, *a: object, **k: object) -> None:
        pass

    async def __aenter__(self) -> _CaptureClient:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def post(self, url: str, json: dict[str, object], headers: object = None) -> _FakeResp:
        _CaptureClient.captured = json
        return _FakeResp()


async def _start_with_zones(
    monkeypatch: pytest.MonkeyPatch, zones: list[dict[str, object]] | None
) -> dict[str, object] | None:
    from sentry_backend.settings import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setenv("SENTRY_AI_URL", "http://node:8001")
    monkeypatch.setenv("MEDIAMTX_RTSP_URL", "rtsp://mtx:8554")
    monkeypatch.setattr(live_ai_client.httpx, "AsyncClient", _CaptureClient)
    _CaptureClient.captured = None
    await live_ai_client.start_worker("cam1", store_id="s1", zones=zones)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    return _CaptureClient.captured


async def test_start_worker_body_includes_zones(monkeypatch: pytest.MonkeyPatch) -> None:
    zones = [{"type": "shelf", "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}]
    body = await _start_with_zones(monkeypatch, zones)
    assert body is not None and body["zones"] == zones


async def test_start_worker_body_omits_zones_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _start_with_zones(monkeypatch, None)
    assert body is not None and "zones" not in body


async def test_start_worker_body_omits_zones_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _start_with_zones(monkeypatch, [])
    assert body is not None and "zones" not in body
