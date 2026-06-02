"""Unit tests for agent token round-trip + pairing-code format (no DB)."""

from uuid import uuid4

from sentry_backend.repository.agent_repo import _gen_code
from sentry_backend.security import create_agent_token, decode_user_token


def test_agent_token_roundtrip() -> None:
    agent_id = uuid4()
    token = create_agent_token(agent_id)
    payload = decode_user_token(token)
    assert payload["typ"] == "agent"
    assert payload["sub"] == str(agent_id)
    assert payload["exp"] > payload["iat"]


def test_pairing_code_is_six_digits() -> None:
    for _ in range(200):
        code = _gen_code()
        assert len(code) == 6
        assert code.isdigit()
