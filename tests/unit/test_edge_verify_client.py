"""ADR-0029 — ai_service.verify_edge_clip POSTs clip BYTES (multipart) to
sentry-ai /v1/edge-clip-upload and returns the verdict, or None when sentry-ai
is unset / non-200 (best-effort; the caller falls back to a behaviour alert).

We monkeypatch ai_service.get_settings so the test needs no backend env."""

from __future__ import annotations

from types import SimpleNamespace

from sentry_backend.services import ai_service


def _settings(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        sentry_ai_url=url, sentry_ai_service_token=None, sentry_ai_timeout_sec=10
    )


class _FakeResp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def post(self, *a, **k) -> _FakeResp:
        return self._resp


async def test_returns_none_when_ai_url_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ai_service, "get_settings", lambda: _settings(""))
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\x00")
    assert await ai_service.verify_edge_clip(clip, store_id="s") is None


async def test_success_returns_verdict(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ai_service, "get_settings", lambda: _settings("http://ai:8001"))
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\x00\x00")
    payload = {
        "category": "pocket_conceal",
        "confidence": 0.8,
        "reasoning": "нуув",
        "model_name": "m",
        "inference_latency_ms": 10,
        "embedding": [0.1],
    }
    monkeypatch.setattr(
        ai_service.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(200, payload))
    )
    out = await ai_service.verify_edge_clip(clip, store_id="s", rag_query="q")
    assert out == payload


async def test_non_200_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ai_service, "get_settings", lambda: _settings("http://ai:8001"))
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\x00")
    monkeypatch.setattr(
        ai_service.httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResp(500, {}))
    )
    assert await ai_service.verify_edge_clip(clip, store_id="s") is None
