"""Inter-service authentication dependency (sentry-ai → sentry-backend, etc.)."""
from typing import Annotated

from fastapi import Header, HTTPException, status

from sentry_backend.security import decode_service_token

SERVICE_AUTH_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid service token",
)


async def require_service_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Validate service-to-service token from `Authorization: Bearer <token>` header.

    Returns the service name extracted from the token.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise SERVICE_AUTH_EXCEPTION
    token = authorization.split(maxsplit=1)[1]
    try:
        payload = decode_service_token(token)
    except ValueError as e:
        raise SERVICE_AUTH_EXCEPTION from e

    if payload.get("typ") != "service":
        raise SERVICE_AUTH_EXCEPTION

    service_name = payload.get("service")
    if not isinstance(service_name, str):
        raise SERVICE_AUTH_EXCEPTION
    return service_name
