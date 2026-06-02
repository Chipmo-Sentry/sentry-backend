"""Lead capture — schema validation, honeypot, and rate-limit keying (no DB)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.requests import Request

from sentry_backend.ratelimit import client_ip_key
from sentry_backend.schemas.lead import LeadCreate


def _request(headers: dict[str, str], client_host: str | None) -> Request:
    scope = {
        "type": "http",
        "headers": Headers(headers).raw,
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


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


def test_rate_limit_key_prefers_forwarded_client() -> None:
    req = _request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "10.0.0.1")
    assert client_ip_key(req) == "203.0.113.7"


def test_rate_limit_key_uses_real_ip_fallback() -> None:
    req = _request({"x-real-ip": "198.51.100.5"}, "10.0.0.1")
    assert client_ip_key(req) == "198.51.100.5"


def test_rate_limit_key_falls_back_to_peer() -> None:
    req = _request({}, "192.0.2.9")
    assert client_ip_key(req) == "192.0.2.9"
