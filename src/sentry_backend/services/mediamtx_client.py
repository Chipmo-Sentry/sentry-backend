"""MediaMTX dynamic path config — POST/DELETE camera paths.

MediaMTX 1.18+ accepts path config updates via:
  POST   /v3/config/paths/add/{name}     body: {"source":..., ...}
  PATCH  /v3/config/paths/patch/{name}   body: {"source":...}
  DELETE /v3/config/paths/delete/{name}

For M1.5 (local laptop), backend has direct HTTP access to MediaMTX's
admin port (default 127.0.0.1:9997). For M2 production, MediaMTX runs
on Hetzner; backend uses MEDIAMTX_API_URL env + optional bearer token.

Failures here MUST NOT block camera CRUD — they're best-effort sync.
The path is also persisted in mediamtx.yml if MediaMTX is misconfigured
or unreachable; an operator can re-run `mediamtx restart` to pick up
all camera rows from the DB-driven config. (M2: backend will rehydrate
MediaMTX on startup from the Camera table.)
"""

from __future__ import annotations

from typing import Any

import httpx

from sentry_backend.logging_setup import get_logger
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.mediamtx_client")

DEFAULT_PATH_CONFIG: dict[str, Any] = {
    "sourceProtocol": "tcp",
    "sourceOnDemand": False,
    "record": True,
}


def _api_base() -> str | None:
    return get_settings().mediamtx_api_url


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    token = get_settings().mediamtx_api_token
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _auth() -> tuple[str, str] | httpx._client.UseClientDefault:
    """HTTP Basic auth for the MediaMTX control API (cloud `api` user).

    Returns httpx's default sentinel when no creds are set (local/dev), so the
    `auth=` kwarg is effectively a no-op there.
    """
    s = get_settings()
    if s.mediamtx_api_user and s.mediamtx_api_pass:
        return (s.mediamtx_api_user, s.mediamtx_api_pass)
    return httpx.USE_CLIENT_DEFAULT


def _path_payload(rtsp_source: str) -> dict[str, Any]:
    """Path config. In publish mode (cloud) we omit `source` so MediaMTX waits
    for the store agent to publish to the path, instead of trying to pull the
    camera RTSP — which is unreachable from the cloud."""
    if get_settings().agent_stream_push_url:
        return dict(DEFAULT_PATH_CONFIG)  # publish mode — no pull source
    return {**DEFAULT_PATH_CONFIG, "source": rtsp_source}


async def add_path(name: str, rtsp_source: str) -> bool:
    """Register a MediaMTX path for `name`.

    Pull mode: source = `rtsp_source` (MediaMTX pulls the camera).
    Publish mode (agent_stream_push_url set): no source — the agent publishes.

    Returns True on success. Never raises — logs and returns False instead.
    Idempotency: if MediaMTX returns "already exists" (400), we PATCH instead.
    """
    base = _api_base()
    if not base:
        log.debug("mediamtx.api_disabled", reason="MEDIAMTX_API_URL not set")
        return False

    payload = _path_payload(rtsp_source)
    url = f"{base.rstrip('/')}/v3/config/paths/add/{name}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(url, json=payload, headers=_headers(), auth=_auth())
        if r.status_code in (200, 201):
            log.info("mediamtx.add_ok", path=name)
            return True
        if r.status_code == 400 and "already exists" in r.text.lower():
            log.info("mediamtx.add_exists_patching", path=name)
            return await patch_path(name, rtsp_source)
        log.warning("mediamtx.add_failed", path=name, status=r.status_code, body=r.text[:200])
    except (httpx.HTTPError, TimeoutError) as e:
        log.warning("mediamtx.add_http_err", path=name, error=str(e))
    return False


async def patch_path(name: str, rtsp_source: str) -> bool:
    base = _api_base()
    if not base:
        return False
    payload = _path_payload(rtsp_source)
    url = f"{base.rstrip('/')}/v3/config/paths/patch/{name}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.patch(url, json=payload, headers=_headers(), auth=_auth())
        if r.status_code in (200, 204):
            log.info("mediamtx.patch_ok", path=name)
            return True
        log.warning("mediamtx.patch_failed", path=name, status=r.status_code, body=r.text[:200])
    except (httpx.HTTPError, TimeoutError) as e:
        log.warning("mediamtx.patch_http_err", path=name, error=str(e))
    return False


async def list_paths() -> dict[str, dict[str, Any]] | None:
    """Read MediaMTX RUNTIME path state via GET /v3/paths/list.

    Returns {path_name: {"ready": bool, "readers": int, "has_source": bool}} —
    `ready` = a publisher is active and the stream is being served (i.e. video is
    actually arriving at the cloud). Returns None when the API is disabled
    (no MEDIAMTX_API_URL) or unreachable, so callers can show "unknown" instead
    of faking "0 paths". Best-effort, never raises.
    """
    base = _api_base()
    if not base:
        return None
    url = f"{base.rstrip('/')}/v3/paths/list"
    out: dict[str, dict[str, Any]] = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            page = 0
            while True:
                r = await client.get(
                    url,
                    params={"itemsPerPage": 1000, "page": page},
                    headers=_headers(),
                    auth=_auth(),
                )
                if r.status_code != 200:
                    log.warning("mediamtx.list_failed", status=r.status_code, body=r.text[:200])
                    return None
                data = r.json()
                for item in data.get("items", []):
                    name = item.get("name")
                    if not isinstance(name, str):
                        continue
                    readers = item.get("readers")
                    out[name] = {
                        "ready": bool(item.get("ready")),
                        "readers": len(readers) if isinstance(readers, list) else 0,
                        "has_source": item.get("source") is not None,
                    }
                page += 1
                if page >= int(data.get("pageCount", 1)):
                    break
    except (httpx.HTTPError, TimeoutError, ValueError) as e:
        log.warning("mediamtx.list_http_err", error=str(e))
        return None
    return out


async def delete_path(name: str) -> bool:
    base = _api_base()
    if not base:
        return False
    url = f"{base.rstrip('/')}/v3/config/paths/delete/{name}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.delete(url, headers=_headers(), auth=_auth())
        if r.status_code in (200, 204):
            log.info("mediamtx.delete_ok", path=name)
            return True
        if r.status_code == 404:
            log.info("mediamtx.delete_not_found", path=name)
            return True  # already gone, treat as success
        log.warning("mediamtx.delete_failed", path=name, status=r.status_code, body=r.text[:200])
    except (httpx.HTTPError, TimeoutError) as e:
        log.warning("mediamtx.delete_http_err", path=name, error=str(e))
    return False
