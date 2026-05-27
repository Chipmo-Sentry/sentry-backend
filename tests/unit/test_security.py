"""Unit tests for security primitives — JWT, password hashing, Fernet."""
from uuid import uuid4

import pytest

from sentry_backend.security import (
    create_service_token,
    create_user_token,
    decode_service_token,
    decode_user_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_then_verify_succeeds(self) -> None:
        pwd = "TheCorrectPassword123!"
        hashed = hash_password(pwd)
        assert verify_password(pwd, hashed) is True

    def test_wrong_password_fails(self) -> None:
        hashed = hash_password("right_password_AAA1!")
        assert verify_password("wrong_password_BBB2!", hashed) is False

    def test_72_byte_clamp_silently_truncates(self) -> None:
        # bcrypt only sees first 72 bytes — we clamp explicitly so the long
        # password and its 72-byte prefix verify to the same hash.
        long = "a" * 100
        prefix = "a" * 72
        hashed = hash_password(long)
        assert verify_password(prefix, hashed) is True


class TestUserTokens:
    def test_access_token_round_trip(self) -> None:
        uid = uuid4()
        token = create_user_token(uid, "access")
        payload = decode_user_token(token)
        assert payload["sub"] == str(uid)
        assert payload["typ"] == "access"

    def test_refresh_token_round_trip(self) -> None:
        uid = uuid4()
        token = create_user_token(uid, "refresh")
        payload = decode_user_token(token)
        assert payload["typ"] == "refresh"

    def test_create_service_via_user_helper_raises(self) -> None:
        with pytest.raises(ValueError):
            create_user_token(uuid4(), "service")  # type: ignore[arg-type]

    def test_garbage_token_raises(self) -> None:
        with pytest.raises(ValueError):
            decode_user_token("not.a.real.jwt")


class TestServiceTokens:
    def test_service_token_round_trip(self) -> None:
        token = create_service_token("sentry-ai")
        payload = decode_service_token(token)
        assert payload["sub"] == "service:sentry-ai"
        assert payload["service"] == "sentry-ai"
        assert payload["typ"] == "service"

    def test_user_token_fails_service_decode(self) -> None:
        # User tokens are signed with a different secret — decode must fail
        user_token = create_user_token(uuid4(), "access")
        with pytest.raises(ValueError):
            decode_service_token(user_token)


class TestFernet:
    def test_encrypt_decrypt_round_trip(self) -> None:
        plaintext = "rtsp://admin:secret@192.168.1.50:554/stream"
        ciphertext = encrypt_secret(plaintext)
        assert ciphertext != plaintext.encode()
        assert decrypt_secret(ciphertext) == plaintext

    def test_ciphertext_is_bytes(self) -> None:
        result = encrypt_secret("hello")
        assert isinstance(result, bytes)
