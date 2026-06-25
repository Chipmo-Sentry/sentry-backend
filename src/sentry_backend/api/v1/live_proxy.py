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
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.repository import ai_node_repo
from sentry_backend.schemas.ai_node import parse_camera_health, parse_hls_base
from sentry_backend.security import decode_user_token

router = APIRouter(prefix="/api/v1/live", tags=["live"])

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


async def _node_hls_base(db: AsyncSession, path: str) -> str | None:
    """The HLS base of the AI node currently serving `path` (matched via the same
    telemetry.cameras[].camera_id link the pipeline view uses)."""
    for node in await ai_node_repo.list_nodes(db):
        cams = parse_camera_health(node.telemetry) or []
        if any(c.camera_id == path for c in cams):
            return parse_hls_base(node.telemetry)
    return None


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
    base = await _node_hls_base(db, path)
    if not base:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No live node for this camera"
        )
    # Forward every query param EXCEPT our own jwt — MediaMTX's low-latency HLS
    # keys sub-playlists and segments on a `?session=…` param, so dropping the
    # query would 401 the stream (the bug that left the player on "H.265?").
    fwd = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "jwt"])
    upstream = f"{base}/{path}/{filename}" + (f"?{fwd}" if fwd else "")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(upstream)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Upstream unreachable: {e}"
        ) from e
    if r.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Upstream {r.status_code}"
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "m3u8":
        body = _rewrite_m3u8(r.text, jwt)
        return Response(content=body, media_type=_CONTENT_TYPES["m3u8"])
    return Response(
        content=r.content,
        media_type=_CONTENT_TYPES.get(ext, "application/octet-stream"),
    )
