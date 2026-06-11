"""T08 — RAG retrieve is actually wired into BOTH verify paths.

verified_case descriptions are the VLM's Mongolian `reasoning` sentences
(embed-at-verify → feedback → case), so the rag_query the backend sends must
be the same kind of text. These tests pin: (a) the query builders produce
Mongolian event descriptions, and (b) both the upload path (`_verify_inner`)
and the live path (`_handle_breach_inner`) include `rag_query` + `store_id`
in the request to sentry-ai.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sentry_backend.services import ai_service, threshold_handler
from sentry_backend.services.ai_service import (
    build_live_rag_query,
    build_upload_rag_query,
)
from sentry_backend.settings import get_settings

# === Builders ===


def test_upload_query_is_mongolian_concealment_text() -> None:
    q = build_upload_rag_query()
    assert q
    assert len(q) <= 2000  # sentry-ai VerifyRequest.rag_query max_length
    # Same register as the VLM reasoning / case descriptions (Mongolian).
    assert "нуусан" in q
    assert "бараа" in q


def test_live_query_includes_risk_and_sequence_phrases() -> None:
    q = build_live_rag_query(
        87.4, ["item_pickup", "wrist_to_torso", "concealment_sequence", "not_a_rule"]
    )
    assert "87%" in q
    assert "бараа авсан" in q
    assert "бугуйгаа цээж" in q
    assert "бүрэн дараалал" in q
    # Unknown rule keys must not leak raw English into the Mongolian query.
    assert "not_a_rule" not in q
    assert len(q) <= 2000


def test_live_query_without_sequences_still_describes_event() -> None:
    q = build_live_rag_query(70.0, None)
    assert "70%" in q
    assert "нуусан" in q
    assert "Илэрсэн үйлдлүүд" not in q


def test_live_query_dedupes_repeated_sequences() -> None:
    q = build_live_rag_query(90.0, ["item_pickup", "item_pickup"])
    assert q.count("бараа авсан") == 1


# === Shared httpx capture fake ===


class _FakeResponse:
    status_code = 503
    text = "ai node down"


class _FakeAsyncClient:
    """Captures the POSTed JSON, then short-circuits with a non-200."""

    captured: dict[str, object] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: D107
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(
        self, url: str, json: dict[str, object] | None = None, headers: object = None
    ) -> _FakeResponse:
        _FakeAsyncClient.captured = {"url": url, "json": json}
        return _FakeResponse()


# === Upload path: _verify_inner sends rag_query ===


async def test_verify_inner_sends_rag_query(monkeypatch: pytest.MonkeyPatch) -> None:
    store_id = uuid4()
    clip = SimpleNamespace(
        storage_path="/clips/x.mp4",
        store_id=store_id,
        camera_id=uuid4(),
        organization_id=uuid4(),
    )

    class _FakeResult:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return clip

    class _FakeDb:
        async def execute(self, stmt: object) -> _FakeResult:
            return _FakeResult()

    @asynccontextmanager
    async def fake_scope():  # type: ignore[no-untyped-def]
        yield _FakeDb()

    monkeypatch.setattr(ai_service, "session_scope", fake_scope)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.captured = {}

    await ai_service._verify_inner(uuid4(), "http://ai.test", 5)

    body = _FakeAsyncClient.captured["json"]
    assert isinstance(body, dict)
    assert str(_FakeAsyncClient.captured["url"]).endswith("/v1/verify")
    # rag_query populated with the Mongolian event description...
    assert body["rag_query"] == build_upload_rag_query()
    # ...and store_id rides along so tenant filtering applies on retrieval.
    assert body["store_id"] == str(store_id)


# === Live path: _handle_breach_inner sends rag_query ===


async def test_handle_breach_inner_sends_rag_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_AI_URL", "http://ai.test")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
        _FakeAsyncClient.captured = {}

        store_id = uuid4()
        camera = SimpleNamespace(
            id=uuid4(), store_id=store_id, mediamtx_path="cam1", risk_threshold=70.0
        )
        handler = threshold_handler.ThresholdHandler()
        await handler._handle_breach_inner(
            camera,  # type: ignore[arg-type]
            person_id=7,
            peak_risk_pct=91.0,
            sequences=["item_pickup", "bag_interaction"],
        )

        body = _FakeAsyncClient.captured["json"]
        assert isinstance(body, dict)
        assert str(_FakeAsyncClient.captured["url"]).endswith("/v1/cut-verify")
        assert body["store_id"] == str(store_id)
        rq = body["rag_query"]
        assert isinstance(rq, str)
        assert "91%" in rq
        assert "бараа авсан" in rq
        assert "цүнх" in rq
    finally:
        get_settings.cache_clear()


def test_person_state_keeps_last_sequences() -> None:
    st = threshold_handler._PersonState()
    assert st.last_sequences == []
    st.last_sequences = ["item_pickup"]
    other = threshold_handler._PersonState()
    # default_factory — no shared mutable default between instances
    assert other.last_sequences == []
