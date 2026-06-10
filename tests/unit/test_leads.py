"""Lead capture — schema validation, honeypot, and rate-limit keying (no DB)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.requests import Request

from sentry_backend.ratelimit import client_ip_key
from sentry_backend.schemas.lead import LeadCreate
from sentry_backend.settings import get_settings


def _request(headers: dict[str, str], client_host: str | None) -> Request:
    scope = {
        "type": "http",
        "headers": Headers(headers).raw,
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


@pytest.fixture()
def hops(monkeypatch: pytest.MonkeyPatch):
    """Set TRUSTED_PROXY_HOPS for a test and reset the settings cache after."""

    def _set(n: int) -> None:
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", str(n))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


def test_lead_requires_valid_email() -> None:
    with pytest.raises(ValidationError):
        LeadCreate(email="not-an-email")


def test_lead_accepts_minimal_payload() -> None:
    lead = LeadCreate(email="owner@shop.mn")
    assert lead.source == "landing"
    assert lead.website is None
    assert lead.camera_count is None


def test_lead_rejects_negative_camera_count() -> None:
    with pytest.raises(ValidationError):
        LeadCreate(email="a@b.mn", camera_count=-1)


def test_honeypot_field_is_captured() -> None:
    lead = LeadCreate(email="bot@spam.io", website="http://spam.example")
    assert lead.website == "http://spam.example"


def test_rate_limit_key_takes_proxy_appended_entry(hops) -> None:
    # With one trusted proxy hop, the real client is the entry the proxy
    # appended (right-most), NOT the left-most one a client could prepend.
    hops(1)
    req = _request({"x-forwarded-for": "203.0.113.7, 198.51.100.1"}, "10.0.0.1")
    assert client_ip_key(req) == "198.51.100.1"


def test_forged_forwarded_for_cannot_create_new_buckets(hops) -> None:
    # Attacker prepends arbitrary, varying XFF entries on each request. The
    # trusted proxy always appends the attacker's true peer (198.51.100.7), so
    # every request must resolve to the SAME bucket key — forgery is defeated.
    hops(1)
    forged = [
        "1.1.1.1, 198.51.100.7",
        "2.2.2.2, 3.3.3.3, 198.51.100.7",
        "evil, 9.9.9.9, 198.51.100.7",
    ]
    keys = {client_ip_key(_request({"x-forwarded-for": v}, "10.0.0.1")) for v in forged}
    assert keys == {"198.51.100.7"}


def test_real_ip_header_is_not_trusted(hops) -> None:
    # X-Real-IP is forgeable and never set by Railway → ignored. With no XFF we
    # fall back to the socket peer, not the attacker-supplied header.
    hops(1)
    req = _request({"x-real-ip": "198.51.100.5"}, "10.0.0.1")
    assert client_ip_key(req) == "10.0.0.1"


def test_short_forwarded_for_falls_back_to_peer(hops) -> None:
    # XFF shorter than the trusted hop count can't be trusted (would expose a
    # forgeable entry) → fall back to the un-forgeable socket peer.
    hops(2)
    req = _request({"x-forwarded-for": "203.0.113.7"}, "192.0.2.9")
    assert client_ip_key(req) == "192.0.2.9"


def test_rate_limit_key_falls_back_to_peer(hops) -> None:
    hops(1)
    req = _request({}, "192.0.2.9")
    assert client_ip_key(req) == "192.0.2.9"


def test_zero_hops_ignores_forwarded_for(hops) -> None:
    # Direct exposure (no proxy): XFF is ignored entirely; only the peer counts.
    hops(0)
    req = _request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "192.0.2.9")
    assert client_ip_key(req) == "192.0.2.9"
