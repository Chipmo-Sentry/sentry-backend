"""Shared slowapi limiter.

The public lead form is reached two ways: a browser → landing SSR → backend
(so `request.client.host` is the landing server, not the visitor), or directly.
We therefore key on the forwarded client IP when present, falling back to the
socket peer. The landing forwards the real visitor IP in `X-Forwarded-For`.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def client_ip_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # First hop is the original client.
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip_key)
