"""Cloudflare Realtime TURN — short-lived ICE-server credentials.

Relaying WebRTC media through Cloudflare's TURN network routes viewers via the
Cloudflare PoP nearest them and Cloudflare's private backbone to the node,
instead of one long lossy public-internet hop (the Mongolia→Korea path). This
is the "Discord model": a nearby edge + a good backbone, which is why distance
stops mattering for smoothness.

We mint short-lived credentials per stream-token request and hand them to the
browser, which passes them to the LiveKit peer connection with
iceTransportPolicy:"relay" so media actually takes the Cloudflare path.

Best-effort: any failure (unconfigured, network, non-201) returns None and the
player falls back to the direct LiveKit/WHEP/HLS path — TURN must never break
live viewing.
"""

from __future__ import annotations

from typing import Any

import httpx

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.cloudflare_turn")

_GENERATE_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"


async def ice_servers(ttl_sec: int = 86400) -> list[dict[str, Any]] | None:
    """Generate Cloudflare TURN ICE servers, or None when unconfigured/failed."""
    settings = get_settings()
    key_id = settings.cloudflare_turn_key_id
    token = settings.cloudflare_turn_api_token
    if not key_id or token is None:
        return None
    url = _GENERATE_URL.format(key_id=key_id)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                url,
                json={"ttl": ttl_sec},
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
        if r.status_code not in (200, 201):
            log.info("cf_turn.non_2xx", status=r.status_code)
            return None
        servers = r.json().get("iceServers")
        if not isinstance(servers, list):
            return None
        return servers
    except (httpx.HTTPError, ValueError) as e:
        log.info("cf_turn.failed", error=str(e))
        return None
