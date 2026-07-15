"""HTTPS live-HLS proxy → each AI node's MediaMTX.

The frontend is HTTPS, but a node's MediaMTX serves HLS over plain HTTP at an
ephemeral vast.ai address — a browser can't load that (mixed content) and the
address changes on every node restart. This proxies live HLS through the (HTTPS,
same-origin) backend: the node self-reports its CURRENT HLS base in telemetry, we
forward playlist/segment requests there, and rewrite playlist URIs so the
short-lived per-camera stream token rides along to every segment fetch.

Read auth = the same stream token minted by GET /api/v1/cameras/{id}/stream-token.
MediaMTX read is anonymous on the node, so the proxy needs no upstream creds.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.agent import Agent
from sentry_backend.db.models.camera import Camera
from sentry_backend.deps.db import get_db
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import ai_node_repo
from sentry_backend.schemas.ai_node import (
    NODE_ONLINE_WINDOW,
    parse_camera_health,
    parse_hls_base,
    parse_served_paths,
)
from sentry_backend.security import decode_user_token

router = APIRouter(prefix="/api/v1/live", tags=["live"])
log = get_logger("sentry_backend.live_proxy")

_CONTENT_TYPES = {
    "m3u8": "application/vnd.apple.mpegurl",
    "ts": "video/mp2t",
    "mp4": "video/mp4",
    "m4s": "video/iso.segment",
}
_URI_TAG = re.compile(r'URI="([^"]*)"')


def _token_ok(token: str, path: str) -> bool:
    if not token:
        return False
    try:
        payload = decode_user_token(token)
    except ValueError:
        return False
    return payload.get("typ") == "stream" and payload.get("path") == path


def _node_online(node: object) -> bool:
    """A node whose last heartbeat is within the online window. A dead node's
    STALE telemetry (old address, old served paths) must never win the match —
    that's how an offline predecessor kept the live HLS pinned to its old HTTP
    address after a node migration."""
    ls = getattr(node, "last_seen_at", None)
    if ls is None:
        return False
    if ls.tzinfo is None:
        ls = ls.replace(tzinfo=UTC)
    return bool(datetime.now(UTC) - ls < NODE_ONLINE_WINDOW)


async def _node_hls_base(db: AsyncSession, path: str) -> str | None:
    """The HLS base of the AI node currently serving `path` (matched via the same
    telemetry.cameras[].camera_id link the pipeline view uses).

    Only ONLINE nodes are considered, and an HTTPS base (redirectable straight to
    the node's own tunnel) is preferred over an HTTP one (which we must proxy) —
    so a lingering offline/HTTP record can't shadow the live node's tunnel."""
    http_fallback: str | None = None
    for node in await ai_node_repo.list_nodes(db):
        if not _node_online(node):
            continue
        cams = parse_camera_health(node.telemetry) or []
        # Match the analysis worker's cameras (cloud topology) OR the node's served
        # MediaMTX paths (edge topology: the node relays but doesn't analyse).
        served = parse_served_paths(node.telemetry)
        if not (any(c.camera_id == path for c in cams) or path in served):
            continue
        base = parse_hls_base(node.telemetry)
        if not base:
            continue
        if base.startswith("https://"):
            return base  # redirectable — the best case, take it immediately
        http_fallback = http_fallback or base
    return http_fallback


# The agent's reported tunnel base is only valid while it's heart-beating (the
# cloudflared tunnel dies with the agent), so ignore a stale one and fall back to
# the node. ~90s tolerates one or two missed 30s heartbeats.
_AGENT_TUNNEL_FRESH_SEC = 90


async def _agent_tunnel_base(db: AsyncSession, path: str) -> str | None:
    """Public HLS base of the store agent serving `path` via its cloudflared
    tunnel, if the agent is currently heart-beating — so the cloud frontend pulls
    video STRAIGHT from the agent and never depends on the (optional) GPU node.
    None → no fresh tunnel; the caller falls back to the node relay."""
    store_id = (
        await db.execute(select(Camera.store_id).where(Camera.mediamtx_path == path))
    ).scalar_one_or_none()
    if store_id is None:
        return None
    cutoff = datetime.now(UTC) - timedelta(seconds=_AGENT_TUNNEL_FRESH_SEC)
    return (
        await db.execute(
            select(Agent.hls_tunnel_base)
            .where(
                Agent.store_id == store_id,
                Agent.hls_tunnel_base.is_not(None),
                Agent.last_seen_at >= cutoff,
            )
            .order_by(Agent.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _with_token(uri: str, jwt: str) -> str:
    # Absolute URIs (shouldn't occur for MediaMTX HLS) are left untouched.
    if not uri or uri.startswith(("http://", "https://")):
        return uri
    return f"{uri}{'&' if '?' in uri else '?'}jwt={jwt}"


def _rewrite_m3u8(text: str, jwt: str) -> str:
    """Append ?jwt= to every URI in a playlist — bare segment lines AND URIs inside
    tags (#EXT-X-MAP / #EXT-X-PART / #EXT-X-PRELOAD-HINT) — so sub-playlist and
    segment requests come back through this authed proxy."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            line = _URI_TAG.sub(lambda m: f'URI="{_with_token(m.group(1), jwt)}"', line)
        elif stripped:
            line = _with_token(line, jwt)
        out.append(line)
    return "\n".join(out) + "\n"


@router.get("/{path}/hls/{filename:path}")
async def hls_proxy(
    path: str,
    filename: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    jwt: Annotated[str, Query()] = "",
) -> Response:
    """Proxy one HLS playlist/segment for `path` from its node's MediaMTX, over
    HTTPS. Playlists are rewritten to keep the token on every nested request."""
    if not _token_ok(jwt, path):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid stream token")
    # PREFER the AI node's MediaMTX. The store agent pushes a BROWSER-SAFE stream to
    # the node (H.265 → H.264 transcode on publish), whereas the agent's own tunnel
    # serves the RAW camera codec off its local fan-out — an H.265 camera then plays
    # on the node but shows "unsupported codec" through the tunnel. So the node is
    # the correct primary whenever it serves the path; the agent tunnel is the
    # fallback for edge/no-node stores.
    base = await _node_hls_base(db, path)
    if base:
        # A node with an HTTPS base has its own tunnel — REDIRECT the browser
        # straight to it (like the agent tunnel). This is the correct path: a
        # direct browser→MediaMTX connection keeps low-latency HLS's IP-bound
        # `?session` consistent (chunked master/sub/segments all from one client),
        # whereas proxying live LL-HLS through the backend churns sessions and
        # stalls the player after ~1s. It also keeps the video off the backend
        # (no egress cost, no single-worker bottleneck). The node's HTTP base is
        # still proxied below — that's LAN/dev where the page isn't HTTPS.
        if base.startswith("https://"):
            fwd = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "jwt"])
            target = f"{base}/{path}/{filename}" + (f"?{fwd}" if fwd else "")
            return RedirectResponse(target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    else:
        # No node serves this path → fall back to the agent's HTTPS cloudflared
        # tunnel. REDIRECT (don't proxy): the tunnel is HTTPS so no mixed content,
        # and a direct browser→MediaMTX session keeps `?session` consistent.
        agent_base = await _agent_tunnel_base(db, path)
        if agent_base:
            fwd = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "jwt"])
            target = f"{agent_base}/{path}/{filename}" + (f"?{fwd}" if fwd else "")
            return RedirectResponse(target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        # Neither a node nor a fresh agent tunnel serves this camera.
        log.warning("hls_proxy.no_node", camera_path=path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Камерт холбогдсон идэвхтэй AI зангилаа алга (зангилаа офлайн байж магадгүй).",
        )
    # HTTP node base (LAN/dev only): the page isn't HTTPS so a redirect to mixed
    # content would be blocked — proxy instead. Safe under a SINGLE web worker
    # (one egress IP for master + sub + segments); if WEB_CONCURRENCY is raised,
    # the node needs an HTTPS tunnel (handled by the redirect branch above).
    # Forward every query param EXCEPT our own jwt — MediaMTX's low-latency HLS
    # keys sub-playlists and segments on a `?session=…` param, so dropping the
    # query would 401 the stream (the bug that left the player on "H.265?").
    fwd = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "jwt"])
    upstream = f"{base}/{path}/{filename}" + (f"?{fwd}" if fwd else "")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(upstream)
    except httpx.HTTPError as e:
        # The node's MediaMTX never answered — the AI node is offline/unreachable
        # (vast.ai instance stopped, network/tunnel down). One clear line, no
        # stack trace, naming the cause + the host we tried.
        log.warning("hls_proxy.node_unreachable", camera_path=path, base=base, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI зангилаа холбогдсонгүй (офлайн). Зангилаагаа эхлүүлээд дахин оролдоно уу.",
        ) from e
    if r.status_code != 200:
        # The node answered but isn't serving this path — the camera isn't
        # publishing to it (agent not pushing, or a path mismatch).
        log.warning("hls_proxy.stream_unavailable", camera_path=path, upstream_status=r.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Камерын стрим зангилаа дээр алга (камер дамжуулж байгаа эсэхийг шалгана уу).",
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "m3u8":
        body = _rewrite_m3u8(r.text, jwt)
        return Response(content=body, media_type=_CONTENT_TYPES["m3u8"])
    return Response(
        content=r.content,
        media_type=_CONTENT_TYPES.get(ext, "application/octet-stream"),
    )
