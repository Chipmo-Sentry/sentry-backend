"""MediaMTX authHTTP authorizer logic — per-camera stream-token read auth."""

from __future__ import annotations

from sentry_backend.api.v1.internal import _creds_match, _stream_token_allows
from sentry_backend.security import create_stream_token


def test_stream_token_allows_matching_path() -> None:
    tok = create_stream_token("cam1_hik")
    assert _stream_token_allows(f"jwt={tok}", "cam1_hik") is True
    # also accepts ?token= alias
    assert _stream_token_allows(f"token={tok}&x=1", "cam1_hik") is True


def test_stream_token_rejects_other_path() -> None:
    tok = create_stream_token("cam1_hik")
    # A token for cam1 must NOT grant access to cam2 (cross-tenant guard).
    assert _stream_token_allows(f"jwt={tok}", "cam2_unv") is False


def test_stream_token_rejects_missing_or_garbage() -> None:
    assert _stream_token_allows("", "cam1") is False
    assert _stream_token_allows("jwt=not-a-jwt", "cam1") is False
    assert _stream_token_allows("foo=bar", "cam1") is False


def test_creds_match() -> None:
    assert _creds_match("u", "p", "u", "p") is True
    assert _creds_match("u", "wrong", "u", "p") is False
    assert _creds_match("u", "p", None, None) is False  # unconfigured → deny
