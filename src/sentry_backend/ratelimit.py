"""Shared slowapi limiter and its client-IP key function.

Rate limits are keyed per client IP. The backend runs behind Railway's edge
proxy, which appends the immediate downstream peer to ``X-Forwarded-For`` (XFF).
A malicious client can *prepend* arbitrary XFF entries, but it cannot control
the entries our own trusted proxies appended. So we never trust the left-most
XFF entry (the classic forgery vector — it lets an attacker mint a fresh
rate-limit bucket per request). Instead we take the entry
``trusted_proxy_hops`` positions from the RIGHT — the IP our trusted proxy
actually observed::

    client → [Railway edge] → backend       XFF = "<client>"         hops=1 → [-1]
    attacker forging XFF=1.2.3.4 → [edge]    XFF = "1.2.3.4, <peer>"  hops=1 → [-1] = <peer>

With ``trusted_proxy_hops = N`` the un-forgeable client-facing IP is
``parts[-N]``. When XFF is absent or shorter than N (local dev with no proxy,
or ``trusted_proxy_hops = 0``) we fall back to the socket peer, which an
external client also cannot forge. ``X-Real-IP`` is intentionally NOT trusted:
Railway does not set it and any client can forge it.

NOTE on the public lead form: sentry-landing's SSR handler forwards the demo
form to ``POST /api/v1/leads`` over the *public* api URL, setting
``X-Forwarded-For: <visitor>``. That adds a second proxy hop (landing + edge),
so with hops=1 those leads key on the landing's egress IP — one shared bucket
rather than per-visitor. To restore per-visitor keying, forward that call over
Railway private networking (no edge hop); then XFF is just ``<visitor>`` and
hops=1 keys it correctly. Do NOT raise the global hop count to 2 to "fix" this:
a direct attacker on the public /leads route could then forge ``parts[-2]``
again, re-defeating every limit.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from sentry_backend.settings import get_settings


def client_ip_key(request: Request) -> str:
    """Return the rate-limit bucket key: the un-forgeable client IP."""
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            parts = [p.strip() for p in fwd.split(",") if p.strip()]
            # The right-most `hops` entries were appended by our trusted
            # proxies; parts[-hops] is the IP the outermost trusted proxy saw.
            # Anything to its left is attacker-controllable and ignored.
            if len(parts) >= hops:
                return parts[-hops]
    # No trusted proxy (hops <= 0) or XFF missing/too short to trust → use the
    # socket peer; an external client cannot forge the TCP source address.
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip_key)
