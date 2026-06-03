"""AI node token round-trip + pairing-code format + config schema (no DB)."""

from uuid import uuid4

from sentry_backend.repository.ai_node_repo import _gen_code
from sentry_backend.schemas.ai_node import AiNodeConfig, AiNodeUpdate
from sentry_backend.security import create_ai_node_token, decode_user_token


def test_ai_node_token_roundtrip() -> None:
    node_id = uuid4()
    token = create_ai_node_token(node_id)
    payload = decode_user_token(token)
    assert payload["typ"] == "ai_node"
    assert payload["sub"] == str(node_id)
    assert payload["exp"] > payload["iat"]


def test_ai_node_token_is_not_an_agent_token() -> None:
    # typ must distinguish node tokens from agent tokens so the auth deps
    # never accept the wrong audience.
    payload = decode_user_token(create_ai_node_token(uuid4()))
    assert payload["typ"] != "agent"


def test_pairing_code_is_six_digits() -> None:
    for _ in range(200):
        code = _gen_code()
        assert len(code) == 6
        assert code.isdigit()


def test_frame_skip_bounds() -> None:
    AiNodeUpdate(frame_skip=0)
    AiNodeUpdate(frame_skip=30)
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AiNodeUpdate(frame_skip=31)


def test_config_defaults_shape() -> None:
    c = AiNodeConfig(enabled=True, provider="minicpm-v-2.6", frame_skip=3)
    assert c.enabled and c.frame_skip == 3
