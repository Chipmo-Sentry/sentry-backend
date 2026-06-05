"""Watch live metadata, fire alerts on sustained risk_pct breaches.

M1-LIVE Phase L5. Wired into POST /api/v1/internal/live-metadata receive path.

Per-(camera_id, person_id) state:
  - rolling risk readings
  - first-breach-timestamp (sustain timer)
  - cooldown timestamp (debounce repeated alerts for same person)

On confirmed breach (sustained > threshold for > sustain_sec, not in cooldown):
  1. Look up Camera by mediamtx_path → org/store/camera UUIDs
  2. cut_window() from MediaMTX recordings
  3. Insert Clip row
  4. Call sentry-ai /v1/verify (synchronously here; we're already off the
     request path because metadata POSTs spawn this in a background task)
  5. Insert Alert with triggered_by=live_threshold, person_id, peak_risk_pct
  6. broker.publish so SSE clients see it
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from sentry_backend.db.models.alert import AlertCategory, AlertTrigger
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.clip import Clip
from sentry_backend.db.models.store import Store
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.services import alert_notify
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.threshold_handler")


# Drop per-person state unseen for this long. ByteTrack assigns new IDs as
# people enter/leave, so without pruning the _state dict grows unbounded over
# a long run (e.g. the 1-hour continuous test).
_STATE_TTL_SEC = 300.0
_CLEANUP_EVERY_SEC = 60.0


@dataclass(slots=True)
class _PersonState:
    risk_pct: float = 0.0
    peak_risk_pct: float = 0.0
    above_threshold_since: float | None = None  # monotonic ts when we first crossed
    last_breach_ts: float = 0.0  # cooldown anchor
    last_seen: float = 0.0  # monotonic ts of last frame


class ThresholdHandler:
    def __init__(self) -> None:
        # Keyed by (camera_id_str, person_id)
        self._state: dict[tuple[str, int], _PersonState] = {}
        self._lock = asyncio.Lock()
        # In-flight breach tasks per camera to avoid spawning duplicates
        self._inflight: set[tuple[str, int]] = set()
        self._last_cleanup: float = 0.0

    async def on_frame(self, frame: dict[str, Any]) -> None:
        """Called from POST /api/v1/internal/live-metadata for each frame.

        frame is the raw dict (FrameMetadata-shaped) — keys: camera_id, tracks.
        Never raises; logs and returns on any error.
        """
        try:
            await self._on_frame_inner(frame)
        except Exception:  # noqa: BLE001
            log.exception("threshold_handler.on_frame_failed")

    async def _on_frame_inner(self, frame: dict[str, Any]) -> None:
        settings = get_settings()
        if not settings.mediamtx_recordings_dir:
            return  # L5 disabled

        cam_path = frame.get("camera_id")
        if not isinstance(cam_path, str):
            return
        tracks = frame.get("tracks") or []
        if not isinstance(tracks, list):
            return

        # Look up Camera row once (might be missing → camera not registered → skip)
        camera = await _resolve_camera_by_mediamtx_path(cam_path)
        if camera is None:
            return

        now = time.monotonic()
        # As of behaviors-PATCH commit: scores are absolute (unbounded) and the
        # color band is driven by the global behavior config's yellow_max.
        # Alerts fire on color=red — keeps L5 trigger in sync with the UI's
        # color thresholds (no separate per-camera risk_threshold).

        async with self._lock:
            self._maybe_cleanup(now)
            for t in tracks:
                pid = t.get("person_id")
                risk = t.get("risk_pct")
                color = t.get("color")
                if not isinstance(pid, int) or not isinstance(risk, (int, float)):
                    continue
                key = (cam_path, pid)
                st = self._state.get(key)
                if st is None:
                    st = _PersonState()
                    self._state[key] = st
                st.risk_pct = float(risk)
                st.peak_risk_pct = max(st.peak_risk_pct, st.risk_pct)
                st.last_seen = now

                # Cooldown still active → don't fire
                if now - st.last_breach_ts < settings.live_breach_cooldown_sec:
                    continue

                if color == "red":
                    if st.above_threshold_since is None:
                        st.above_threshold_since = now
                    elif (
                        now - st.above_threshold_since >= settings.live_breach_sustain_sec
                        and key not in self._inflight
                    ):
                        # Confirmed sustained breach
                        peak = st.peak_risk_pct
                        st.last_breach_ts = now
                        st.above_threshold_since = None
                        st.peak_risk_pct = 0.0
                        self._inflight.add(key)
                        asyncio.create_task(self._handle_breach(camera, pid, peak, key))
                else:
                    st.above_threshold_since = None

    def _maybe_cleanup(self, now: float) -> None:
        """Prune person states unseen past _STATE_TTL_SEC. Caller holds the lock.

        Runs at most once per _CLEANUP_EVERY_SEC. Also clears any orphaned
        _inflight keys whose state has been pruned (defensive — _handle_breach
        normally discards them).
        """
        if now - self._last_cleanup < _CLEANUP_EVERY_SEC:
            return
        self._last_cleanup = now
        stale = [k for k, st in self._state.items() if now - st.last_seen > _STATE_TTL_SEC]
        for k in stale:
            del self._state[k]
            self._inflight.discard(k)
        if stale:
            log.debug("threshold_handler.cleanup", pruned=len(stale), remaining=len(self._state))

    async def _handle_breach(
        self,
        camera: Camera,
        person_id: int,
        peak_risk_pct: float,
        key: tuple[str, int],
    ) -> None:
        try:
            await self._handle_breach_inner(camera, person_id, peak_risk_pct)
        except Exception:  # noqa: BLE001
            log.exception(
                "threshold_handler.breach_failed",
                camera_id=str(camera.id),
                person_id=person_id,
            )
        finally:
            async with self._lock:
                self._inflight.discard(key)

    async def _handle_breach_inner(
        self, camera: Camera, person_id: int, peak_risk_pct: float
    ) -> None:
        settings = get_settings()
        cam_path = camera.mediamtx_path
        if not cam_path:
            return

        log.info(
            "threshold_handler.breach_start",
            camera_path=cam_path,
            person_id=person_id,
            peak_risk_pct=peak_risk_pct,
        )

        # 1. Ask the AI node to cut the breach clip from ITS recordings AND
        # verify it (split topology: the Railway backend can't read the node's
        # MediaMTX recordings, so the cut + VLM both run on the node, which
        # returns the verdict + the clip bytes — docs/19 I5).
        ai_url = settings.sentry_ai_url
        if not ai_url:
            log.warning("threshold_handler.no_ai_url")
            return
        try:
            async with httpx.AsyncClient(timeout=settings.sentry_ai_timeout_sec) as client:
                resp = await client.post(
                    f"{ai_url}/v1/cut-verify",
                    json={
                        "mediamtx_path": cam_path,
                        "store_id": str(camera.store_id) if camera.store_id else None,
                        "camera_id": str(camera.id),
                        "person_id": person_id,
                        "peak_risk_pct": peak_risk_pct,
                    },
                )
        except httpx.HTTPError as e:
            log.warning("threshold_handler.cutverify_http_err", error=str(e))
            return
        if resp.status_code != 200:
            log.warning(
                "threshold_handler.cutverify_non_200",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return
        data: dict[str, Any] = resp.json()

        # 2. Persist the returned clip on the backend, then insert the Clip row.
        clip_bytes = base64.b64decode(data["clip_b64"])
        out_dir = Path(settings.clip_storage_dir).resolve()  # noqa: ASYNC240
        out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        out_path = out_dir / f"live_{uuid4().hex}.mp4"
        out_path.write_bytes(clip_bytes)  # noqa: ASYNC230
        sha_hex = hashlib.sha256(clip_bytes).hexdigest()
        captured_at = datetime.fromisoformat(data["captured_at"])

        clip_id: UUID
        org_id_resolved: UUID
        async with session_scope() as db:
            cam_loaded = await db.get(Camera, camera.id)
            if cam_loaded is None:
                log.error("threshold_handler.camera_vanished", camera_id=str(camera.id))
                return
            store = await db.get(Store, cam_loaded.store_id)
            if store is None:
                log.error("threshold_handler.store_vanished", store_id=str(cam_loaded.store_id))
                return
            org_id_resolved = store.organization_id
            clip = Clip(
                organization_id=org_id_resolved,
                store_id=cam_loaded.store_id,
                camera_id=cam_loaded.id,
                captured_at=captured_at,
                duration_sec=float(data["duration_sec_clip"]),
                storage_path=out_path.as_posix(),
                file_size_bytes=int(data["file_size_bytes"]),
                sha256=sha_hex,
            )
            db.add(clip)
            await db.flush()
            clip_id = clip.id

        # 3. Build alert fields from the verify result.
        category = AlertCategory.other
        confidence = 0.0
        reasoning = (
            f"Live threshold breach — VLM parse error. Person #{person_id} "
            f"peak risk {peak_risk_pct:.0f}%."
        )
        model_name = "n/a"
        latency = 0
        embedding: list[float] | None = None
        with contextlib.suppress(KeyError, ValueError):
            category = AlertCategory(data["category"])
            confidence = float(data["confidence"])
            reasoning = str(data["reasoning"])
            model_name = str(data["model_name"])
            latency = int(data["inference_latency_ms"])
        if isinstance(data.get("embedding"), list):
            embedding = [float(x) for x in data["embedding"]]

        level = derive_alert_level(category, confidence)

        # 4. Insert Alert with full L5 context (+ embedding for the RAG loop).
        alert_public: AlertPublic | None = None
        async with session_scope() as db:
            alert = await alert_repo.create_alert(
                db,
                clip_id=clip_id,
                organization_id=org_id_resolved,
                store_id=camera.store_id,
                camera_id=camera.id,
                category=category,
                confidence=confidence,
                reasoning=reasoning,
                model_name=model_name,
                alert_level=level,
                inference_latency_ms=latency,
                triggered_by=AlertTrigger.live_threshold,
                person_id=person_id,
                peak_risk_pct=peak_risk_pct,
                embedding=embedding,
            )
            alert_public = AlertPublic.model_validate(alert)
            # BE1 — best-effort Telegram ping for the live breach.
            await alert_notify.notify_alert(db, alert)

        # 6. Publish to SSE
        if alert_public is not None:
            await get_broker().publish(
                alert_public.organization_id,
                alert_public.model_dump(mode="json"),
            )
            log.info(
                "threshold_handler.alert_created",
                alert_id=str(alert_public.id),
                camera_path=cam_path,
                person_id=person_id,
                peak_risk_pct=peak_risk_pct,
                category=str(category),
                level=str(level),
            )


# === Helpers ===


async def _resolve_camera_by_mediamtx_path(path: str) -> Camera | None:
    async with session_scope() as db:
        result = await db.execute(select(Camera).where(Camera.mediamtx_path == path))
        return result.scalar_one_or_none()


# === Singleton ===

_handler: ThresholdHandler | None = None


def get_threshold_handler() -> ThresholdHandler:
    global _handler
    if _handler is None:
        _handler = ThresholdHandler()
    return _handler
