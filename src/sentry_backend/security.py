"""JWT, password hashing, cookies, Fernet, service tokens."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import Response
from jose import JWTError, jwt
from passlib.context import CryptContext

from sentry_backend.settings import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    # bcrypt has a 72-byte limit; clamp explicitly to avoid silent truncation
    pw_bytes = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    return _pwd_context.hash(pw_bytes)


def verify_password(password: str, hashed: str) -> bool:
    pw_bytes = password.encode("utf-8")[:BCRYPT_MAX_BYTES]
    return _pwd_context.verify(pw_bytes, hashed)


TokenType = Literal["access", "refresh", "service"]


def _now_utc() -> datetime:
    return datetime.now(UTC)


def create_user_token(
    user_id: UUID,
    token_type: TokenType,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = _now_utc()
    if token_type == "access":
        exp = now + timedelta(minutes=settings.jwt_access_ttl_minutes)
    elif token_type == "refresh":
        exp = now + timedelta(days=settings.jwt_refresh_ttl_days)
    else:
        raise ValueError("Use create_service_token for service tokens")

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": token_type,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_service_token(service_name: str) -> str:
    settings = get_settings()
    now = _now_utc()
    payload: dict[str, Any] = {
        "sub": f"service:{service_name}",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.service_token_ttl_days)).timestamp()),
        "typ": "service",
        "service": service_name,
    }
    return jwt.encode(
        payload,
        settings.service_token_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_user_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}") from e


def decode_service_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.service_token_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as e:
        raise ValueError(f"Invalid service token: {e}") from e


def create_stream_token(mediamtx_path: str) -> str:
    """Short-lived, per-path read token for WHEP/HLS playback.

    Minted by the backend for an authenticated org member who owns the camera,
    then appended to the MediaMTX stream URL (?jwt=…) and validated by the
    backend's MediaMTX authHTTP endpoint. Replaces MediaMTX's `user: any` open
    read so tenants can't watch each other's cameras. Signed with the user
    secret; carries `typ=stream` + the allowed `path`.
    """
    settings = get_settings()
    now = _now_utc()
    exp = now + timedelta(seconds=settings.stream_token_ttl_sec)
    payload: dict[str, Any] = {
        "sub": mediamtx_path,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": "stream",
        "path": mediamtx_path,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


AGENT_TOKEN_TTL_DAYS = 365


def create_agent_token(agent_id: UUID) -> str:
    """Long-lived JWT for a paired camera-relay agent. Signed with the same
    secret as user tokens but carries ``typ=agent``; revocation is enforced by
    checking ``Agent.is_active`` at request time (decode via decode_user_token)."""
    settings = get_settings()
    now = _now_utc()
    payload: dict[str, Any] = {
        "sub": str(agent_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=AGENT_TOKEN_TTL_DAYS)).timestamp()),
        "typ": "agent",
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


AI_NODE_TOKEN_TTL_DAYS = 365


def create_ai_node_token(ai_node_id: UUID) -> str:
    """Long-lived JWT for a paired AI compute node (Predator/Hetzner). Carries
    ``typ=ai_node``; revocation enforced via ``AiNode.is_active`` at request time."""
    settings = get_settings()
    now = _now_utc()
    payload: dict[str, Any] = {
        "sub": str(ai_node_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=AI_NODE_TOKEN_TTL_DAYS)).timestamp()),
        "typ": "ai_node",
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


ACCESS_COOKIE_NAME = "sentry_access"
REFRESH_COOKIE_NAME = "sentry_refresh"


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    settings = get_settings()
    samesite = settings.cookie_samesite
    # SameSite=None is only valid on a Secure cookie; force secure in that case
    # (also secure in production regardless).
    secure = settings.environment == "production" or samesite == "none"
    domain = settings.cookie_domain or None  # empty string → host-only cookie
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        domain=domain,
        max_age=settings.jwt_access_ttl_minutes * 60,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        domain=domain,
        max_age=settings.jwt_refresh_ttl_days * 86400,
        path="/api/v1/auth",
    )


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    domain = settings.cookie_domain or None
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/", domain=domain)
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth", domain=domain)


def _get_fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.rtsp_fernet_key.get_secret_value().encode())


def encrypt_secret(plaintext: str) -> bytes:
    return _get_fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes) -> str:
    return _get_fernet().decrypt(ciphertext).decode()
