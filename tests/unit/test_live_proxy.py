"""Unit tests for the live HLS proxy helpers (token gate + playlist rewrite)."""

from __future__ import annotations

from sentry_backend.api.v1.live_proxy import _rewrite_m3u8, _token_ok, _with_token
from sentry_backend.security import create_stream_token


def test_token_ok_accepts_matching_path() -> None:
    tok = create_stream_token("cam_a")
    assert _token_ok(tok, "cam_a") is True


def test_token_ok_rejects_wrong_path_and_garbage() -> None:
    tok = create_stream_token("cam_a")
    assert _token_ok(tok, "cam_b") is False
    assert _token_ok("not-a-token", "cam_a") is False
    assert _token_ok("", "cam_a") is False


def test_with_token_appends_query() -> None:
    assert _with_token("seg0.ts", "JWT") == "seg0.ts?jwt=JWT"
    assert _with_token("seg0.ts?x=1", "JWT") == "seg0.ts?x=1&jwt=JWT"
    # absolute URIs are left untouched
    assert _with_token("http://x/seg.ts", "JWT") == "http://x/seg.ts"


def test_rewrite_m3u8_tokenizes_bare_lines_and_tag_uris() -> None:
    src = "\n".join(
        [
            "#EXTM3U",
            '#EXT-X-MAP:URI="init.mp4"',
            "#EXTINF:2.0,",
            "seg0.ts",
        ]
    )
    out = _rewrite_m3u8(src, "JWT")
    assert '#EXT-X-MAP:URI="init.mp4?jwt=JWT"' in out
    assert "seg0.ts?jwt=JWT" in out
    # comment/directive lines without a URI stay intact
    assert "#EXTINF:2.0," in out
