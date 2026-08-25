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
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from sentry_backend.db.models.alert import AlertCategory, AlertLevel, AlertTrigger
from sentry_backend.db.models.camera import Camera
from sentry_backend.db.models.clip import Clip
from sentry_backend.db.models.event_log import EventSeverity, EventType
from sentry_backend.db.models.store import Store
from sentry_backend.db.session import session_scope
from sentry_backend.logging_setup import get_logger
from sentry_backend.repository import alert_repo
from sentry_backend.schemas.alert import AlertPublic
from sentry_backend.services import alert_notify, event_log
from sentry_backend.services.ai_service import build_live_rag_query
from sentry_backend.services.alert_broker import get_broker
from sentry_backend.services.alert_service import derive_alert_level
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.threshold_handler")


# Drop per-person state unseen for this long. ByteTrack assigns new IDs as
# people enter/leave, so without pruning the _state dict grows unbounded over
# a long run (e.g. the 1-hour continuous test).
_STATE_TTL_SEC = 300.0
_CLEANUP_EVERY_SEC = 60.0

# Only log an episode summary if the person reached at least the yellow band
# (>10) — green-only passers-by would flood the activity log otherwise.
_EPISODE_LOG_MIN_PCT = 10.0

# Risk-level bands, mirroring the behaviour engine (sentry-ai behavior.py).
_LEVEL_LOW_MAX = 10.0
_LEVEL_MEDIUM_MAX = 25.0
_LEVEL_HIGH_MAX = 50.0


def _risk_level(pct: float) -> str:
    if pct <= _LEVEL_LOW_MAX:
        return "LOW"
    if pct <= _LEVEL_MEDIUM_MAX:
        return "MEDIUM"
    if pct <= _LEVEL_HIGH_MAX:
        return "HIGH"
    return "CRITICAL"


@dataclass(slots=True)
class _EpisodeSummary:
    """A finished live episode worth recording on the activity log."""

    cam_path: str
    person_id: int
    peak_risk_pct: float
    behaviors: list[str]
    behavior_scores: dict[str, float]
    alerted: bool
    duration_sec: float
    # Normalized camera-image foot point at the episode's PEAK risk — projected
    # onto the floor plan at summary time so the risk analytics can heatmap
    # WHERE incidents cluster, not just when.
    peak_foot: tuple[float, float] | None = None


@dataclass(slots=True)
class _PersonState:
    risk_pct: float = 0.0
    peak_risk_pct: float = 0.0
    # Lifetime peak for this track — UNLIKE peak_risk_pct it is never reset by a
    # breach, so the episode-summary row (emitted on prune) reports the true high
    # water mark. first_seen anchors the episode duration; alerted records
    # whether this episode produced an Alert (so the summary can say so).
    episode_peak_risk_pct: float = 0.0
    first_seen: float = 0.0
    alerted: bool = False
    above_threshold_since: float | None = None  # monotonic ts when we first crossed
    last_breach_ts: float = 0.0  # cooldown anchor
    last_seen: float = 0.0  # monotonic ts of last frame
    # Latest non-empty behavior-engine sequence rule keys for this track —
    # carried into the breach so the RAG query (T08) can describe the event.
    last_sequences: list[str] = field(default_factory=list)
    # Episode context from the v2 behavior engine: fired criterion keys
    # (first-fired order) and when the episode's first criterion fired (node
    # epoch ms). Both stored on the Alert and used to size the breach clip so
    # it covers the WHOLE episode, not a fixed [-5s, +10s].
    last_behaviors: list[str] = field(default_factory=list)
    # Latest non-empty per-criterion banked score + seconds-from-episode-start,
    # carried into the breach to build the alert's behaviour timeline.
    last_behavior_scores: dict[str, float] = field(default_factory=dict)
    last_behavior_offsets: dict[str, float] = field(default_factory=dict)
    episode_started_ms: int | None = None
    # Foot point (normalized image coords) captured when episode_peak_risk_pct
    # was last raised — i.e. where the person stood at their riskiest moment.
    peak_foot: tuple[float, float] | None = None


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
        # L5 alert-gate. The breach clip is cut + VLM-verified ON THE AI NODE
        # (POST {sentry_ai_url}/v1/cut-verify — see _handle_breach_inner), because
        # in the split/Railway topology the backend can't read the node's MediaMTX
        # recordings. So ALERTS require the AI node to be configured. We still
        # TRACK per-person risk + write episode summaries (#2) without it — that
        # only needs the live metadata, which already flows (the overlay works).
        l5_enabled = bool(settings.sentry_ai_url)

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
        # ADR-0022: risk_pct is normalized 0-100. A breach is risk_pct crossing
        # the per-camera risk_threshold (default 50), sustained for N seconds.
        # Every breach is then VLM-confirmed before any alert/notification fires.

        async with self._lock:
            episode_summaries = self._maybe_cleanup(now)
            for t in tracks:
                pid = t.get("person_id")
                risk = t.get("risk_pct")
                if not isinstance(pid, int) or not isinstance(risk, (int, float)):
                    continue
                key = (cam_path, pid)
                st = self._state.get(key)
                if st is None:
                    st = _PersonState()
                    st.first_seen = now
                    self._state[key] = st
                st.risk_pct = float(risk)
                st.peak_risk_pct = max(st.peak_risk_pct, st.risk_pct)
                if st.risk_pct >= st.episode_peak_risk_pct and st.risk_pct > 0:
                    # New episode high-water mark → remember WHERE they stood
                    # (normalized bottom-center of the box = foot point).
                    box = t.get("box")
                    if isinstance(box, (list, tuple)) and len(box) == 4:
                        try:
                            st.peak_foot = (
                                (float(box[0]) + float(box[2])) / 2.0,
                                float(box[3]),
                            )
                        except (TypeError, ValueError):
                            pass
                st.episode_peak_risk_pct = max(st.episode_peak_risk_pct, st.risk_pct)
                st.last_seen = now
                # Remember the latest fired sequence rules (RAG query context).
                seqs = t.get("sequences")
                if isinstance(seqs, list) and seqs:
                    st.last_sequences = [str(s) for s in seqs]
                # Episode context: fired criterion keys + when the episode opened.
                # Kept "last non-empty" so the values that caused the breach
                # survive even if an IDLE reset lands before the breach fires.
                behs = t.get("behaviors")
                if isinstance(behs, list) and behs:
                    st.last_behaviors = [str(b) for b in behs]
                scores = t.get("behavior_scores")
                if isinstance(scores, dict) and scores:
                    st.last_behavior_scores = {str(k): float(v) for k, v in scores.items()}
                offsets = t.get("behavior_offsets")
                if isinstance(offsets, dict) and offsets:
                    st.last_behavior_offsets = {str(k): float(v) for k, v in offsets.items()}
                ep_ms = t.get("episode_started_ms")
                if isinstance(ep_ms, int) and ep_ms > 0:
                    st.episode_started_ms = ep_ms

                # Node-push topology: the node detects breaches + cuts/verifies +
                # pushes the finished alert itself. The backend only tracks risk
                # (above) and writes episode summaries (on prune) — it must NOT
                # also pull /v1/cut-verify, or we'd double-handle every breach.
                if settings.live_alerts_via_node_push:
                    continue

                # Cooldown still active → don't fire
                if now - st.last_breach_ts < settings.live_breach_cooldown_sec:
                    continue

                # Breach when the normalized risk_pct (0-100, ADR-0022) crosses
                # this camera's tunable threshold (default 50). `color` is kept
                # only for the live overlay.
                if st.risk_pct >= camera.risk_threshold:
                    if st.above_threshold_since is None:
                        st.above_threshold_since = now
                    elif (
                        now - st.above_threshold_since >= settings.live_breach_sustain_sec
                        and key not in self._inflight
                    ):
                        # Confirmed sustained breach.
                        peak = st.peak_risk_pct
                        st.last_breach_ts = now  # throttle (also throttles the diag below)
                        st.above_threshold_since = None
                        st.peak_risk_pct = 0.0
                        if not l5_enabled:
                            # Breach-worthy but no AI node to cut/verify → say so
                            # in the log so the operator sees WHY no alert appears,
                            # instead of silent nothing.
                            asyncio.create_task(
                                self._emit_alert_blocked(
                                    camera,
                                    pid,
                                    peak,
                                    "AI node тохируулаагүй тул сэрэмжлүүлэг үүсгэх "
                                    "боломжгүй (SENTRY_AI_URL).",
                                )
                            )
                        else:
                            sequences = list(st.last_sequences)
                            behaviors = list(st.last_behaviors)
                            behavior_detail = build_behavior_detail(
                                behaviors, st.last_behavior_scores, st.last_behavior_offsets
                            )
                            # Node-clock anchor for the clip window: the breaching
                            # frame's capture ts (same clock as episode_started_ms).
                            frame_ts = frame.get("ts_ms")
                            breach_ts_ms = (
                                frame_ts
                                if isinstance(frame_ts, int) and frame_ts > 0
                                else int(time.time() * 1000)
                            )
                            episode_ms = st.episode_started_ms
                            # NB: st.alerted is set only when the alert is really
                            # created (in _handle_breach_inner), not here — the
                            # cut/VLM can still fail, in which case no alert exists.
                            self._inflight.add(key)
                            asyncio.create_task(
                                self._handle_breach(
                                    camera,
                                    pid,
                                    peak,
                                    key,
                                    sequences,
                                    behaviors=behaviors,
                                    behavior_detail=behavior_detail,
                                    episode_started_ms=episode_ms,
                                    breach_ts_ms=breach_ts_ms,
                                )
                            )
                else:
                    st.above_threshold_since = None

        # Episode summaries for pruned tracks are written OUTSIDE the lock (DB IO)
        # and fire-and-forget so they never slow frame processing.
        for summary in episode_summaries:
            asyncio.create_task(self._emit_episode_summary(summary))

    def _maybe_cleanup(self, now: float) -> list[_EpisodeSummary]:
        """Prune person states unseen past _STATE_TTL_SEC. Caller holds the lock.

        Runs at most once per _CLEANUP_EVERY_SEC. Also clears any orphaned
        _inflight keys whose state has been pruned (defensive — _handle_breach
        normally discards them). Returns an episode-summary for each pruned track
        whose peak risk reached at least the yellow band, so the caller can write
        them to the activity log (#2: see live scoring even without an alert).
        """
        if now - self._last_cleanup < _CLEANUP_EVERY_SEC:
            return []
        self._last_cleanup = now
        stale = [k for k, st in self._state.items() if now - st.last_seen > _STATE_TTL_SEC]
        summaries: list[_EpisodeSummary] = []
        for k in stale:
            st = self._state[k]
            if st.episode_peak_risk_pct > _EPISODE_LOG_MIN_PCT:
                cam_path, pid = k
                summaries.append(
                    _EpisodeSummary(
                        cam_path=cam_path,
                        person_id=pid,
                        peak_risk_pct=st.episode_peak_risk_pct,
                        behaviors=list(st.last_behaviors),
                        behavior_scores=dict(st.last_behavior_scores),
                        alerted=st.alerted,
                        duration_sec=max(0.0, st.last_seen - st.first_seen),
                        peak_foot=st.peak_foot,
                    )
                )
            del self._state[k]
            self._inflight.discard(k)
        if stale:
            log.debug("threshold_handler.cleanup", pruned=len(stale), remaining=len(self._state))
        return summaries

    async def _emit_episode_summary(self, s: _EpisodeSummary) -> None:
        """Write a risk_episode row for a finished episode (#2: live-scoring
        visibility — peak risk + which behaviours fired, even with no alert)."""
        try:
            async with session_scope() as db:
                camera = (
                    await db.execute(select(Camera).where(Camera.mediamtx_path == s.cam_path))
                ).scalar_one_or_none()
                if camera is None:
                    return
                store = await db.get(Store, camera.store_id)
                org_id = store.organization_id if store else None
                # Project the peak-risk foot point onto the floor plan (normalized
                # 0-1) so risk analytics can heatmap incident locations. None when
                # the camera isn't calibrated on the plan — the analytics fall
                # back to camera-level grouping for such episodes.
                plan_pos: list[float] | None = None
                if s.peak_foot is not None and store is not None and store.floor_plan:
                    from sentry_backend.services.footfall_aggregator import project_foot_to_plan

                    proj = project_foot_to_plan(
                        store.floor_plan, s.cam_path, s.peak_foot[0], s.peak_foot[1], clip=False
                    )
                    if proj is not None:
                        (px, py), (sx, sy) = proj
                        if sx > 0 and sy > 0:
                            plan_pos = [round(px / sx, 4), round(py / sy, 4)]
                level = _risk_level(s.peak_risk_pct)
                severity = {
                    "MEDIUM": EventSeverity.info,
                    "HIGH": EventSeverity.warning,
                    "CRITICAL": EventSeverity.critical,
                }.get(level, EventSeverity.info)
                behs = ", ".join(s.behaviors) if s.behaviors else "—"
                tail = " — сэрэмжлүүлэг үүссэн" if s.alerted else ""
                await event_log.emit(
                    db,
                    event_type=EventType.risk_episode,
                    severity=severity,
                    message=(
                        f"Объект {camera.name}: оргил эрсдэл {s.peak_risk_pct:.0f}% "
                        f"({level}) — {behs}{tail}"
                    ),
                    organization_id=org_id,
                    store_id=camera.store_id,
                    camera_id=camera.id,
                    actor_label="AI",
                    detail={
                        "person_id": s.person_id,
                        "peak_risk_pct": round(s.peak_risk_pct, 1),
                        "level": level,
                        "behaviors": s.behaviors,
                        "behavior_scores": s.behavior_scores,
                        "alerted": s.alerted,
                        "duration_sec": round(s.duration_sec, 1),
                        "plan_pos": plan_pos,
                    },
                )
        except Exception:  # noqa: BLE001 — summary logging must never disrupt L5
            log.warning("threshold_handler.episode_summary_failed", exc_info=True)

    async def _emit_alert_blocked(
        self, camera: Camera, person_id: int, peak: float, reason: str
    ) -> None:
        """Activity-log row explaining why a sustained breach produced NO alert
        (#3 diagnosis): the operator sees the exact gate that blocked it instead
        of silent nothing."""
        try:
            async with session_scope() as db:
                store = await db.get(Store, camera.store_id)
                org_id = store.organization_id if store else None
                await event_log.emit(
                    db,
                    event_type=EventType.error,
                    severity=EventSeverity.warning,
                    message=f"Объект {camera.name}: эрсдэл {peak:.0f}% давсан — {reason}",
                    organization_id=org_id,
                    store_id=camera.store_id,
                    camera_id=camera.id,
                    actor_label="AI",
                    detail={"person_id": person_id, "peak_risk_pct": round(peak, 1)},
                )
        except Exception:  # noqa: BLE001
            log.warning("threshold_handler.alert_blocked_emit_failed", exc_info=True)

    async def _handle_breach(
        self,
        camera: Camera,
        person_id: int,
        peak_risk_pct: float,
        key: tuple[str, int],
        sequences: list[str] | None = None,
        *,
        behaviors: list[str] | None = None,
        behavior_detail: list[dict[str, Any]] | None = None,
        episode_started_ms: int | None = None,
        breach_ts_ms: int | None = None,
    ) -> None:
        try:
            await self._handle_breach_inner(
                camera,
                person_id,
                peak_risk_pct,
                sequences,
                behaviors=behaviors,
                behavior_detail=behavior_detail,
                episode_started_ms=episode_started_ms,
                breach_ts_ms=breach_ts_ms,
            )
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
        self,
        camera: Camera,
        person_id: int,
        peak_risk_pct: float,
        sequences: list[str] | None = None,
        *,
        behaviors: list[str] | None = None,
        behavior_detail: list[dict[str, Any]] | None = None,
        episode_started_ms: int | None = None,
        breach_ts_ms: int | None = None,
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
            behaviors=behaviors,
            episode_started_ms=episode_started_ms,
        )

        # Let the act COMPLETE before cutting: the recording only contains
        # footage up to "now", so cutting at breach time would truncate the clip
        # at the breach moment despite the +post-roll in the requested window.
        post_roll = max(0.0, settings.live_breach_post_roll_sec)
        if post_roll:
            await asyncio.sleep(post_roll)

        # Dynamic window: cover the whole episode (first criterion → breach →
        # post-roll) instead of a fixed [-5s, +10s].
        start_offset_sec, duration_sec = compute_clip_window(
            breach_ts_ms=breach_ts_ms,
            episode_started_ms=episode_started_ms,
            pre_pad_sec=settings.live_clip_pre_pad_sec,
            post_roll_sec=post_roll,
            max_sec=settings.live_clip_max_sec,
        )

        # 1. Ask the AI node to cut the breach clip from ITS recordings AND
        # verify it (split topology: the Railway backend can't read the node's
        # MediaMTX recordings, so the cut + VLM both run on the node, which
        # returns the verdict + the clip bytes — docs/19 I5).
        ai_url = settings.sentry_ai_url
        if not ai_url:
            log.warning("threshold_handler.no_ai_url")
            return
        # Same service-token header as the /v1/verify path (ai_service) — the
        # cut-verify call previously sent none, which breaks once the node
        # enforces AI_SERVICE_TOKEN.
        token = settings.sentry_ai_service_token
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            async with httpx.AsyncClient(timeout=settings.sentry_ai_timeout_sec) as client:
                resp = await client.post(
                    f"{ai_url}/v1/cut-verify",
                    headers=headers,
                    json={
                        "mediamtx_path": cam_path,
                        "store_id": str(camera.store_id) if camera.store_id else None,
                        "camera_id": str(camera.id),
                        "person_id": person_id,
                        "peak_risk_pct": peak_risk_pct,
                        "start_offset_sec": start_offset_sec,
                        "duration_sec": duration_sec,
                        # RAG (T08): Mongolian event description in the same
                        # register as verified_case descriptions (VLM reasoning)
                        # so the node retrieves this store's similar past cases.
                        "rag_query": build_live_rag_query(peak_risk_pct, sequences),
                    },
                )
        except httpx.HTTPError as e:
            log.warning("threshold_handler.cutverify_http_err", error=str(e))
            await self._emit_alert_blocked(
                camera, person_id, peak_risk_pct, "AI node-той холбогдож чадсангүй"
            )
            return
        if resp.status_code != 200:
            log.warning(
                "threshold_handler.cutverify_non_200",
                status=resp.status_code,
                body=resp.text[:200],
            )
            await self._emit_alert_blocked(
                camera,
                person_id,
                peak_risk_pct,
                f"Clip огтлох/баталгаажуулах амжилтгүй (HTTP {resp.status_code})",
            )
            return
        data: dict[str, Any] = resp.json()

        # 1b. VLM gate (ADR-0022) — parse the verdict FIRST. If the VLM is
        # confident there's nothing (ignore: browsing / low confidence), drop the
        # breach entirely: no clip stored, no alert row, no notification. This is
        # what makes "every alert is VLM-confirmed" true and keeps false
        # positives out of the alert list.
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
        if level == AlertLevel.ignore:
            log.info(
                "threshold_handler.vlm_cleared",
                camera_path=cam_path,
                person_id=person_id,
                category=category.value,
                confidence=confidence,
            )
            return

        # 2. Persist the returned clip on the backend, then insert the Clip row.
        max_bytes = settings.max_clip_size_mb * 1024 * 1024
        clip_b64 = str(data["clip_b64"])
        # Reject before decode: base64 inflates ~4/3, so a string longer than
        # ceil(max_bytes/3)*4 cannot decode under the cap — cheap guard against a
        # rogue/compromised AI node spilling unbounded bytes onto disk.
        if len(clip_b64) > ((max_bytes + 2) // 3) * 4:
            log.error(
                "threshold_handler.clip_too_large",
                camera_path=cam_path,
                person_id=person_id,
                b64_len=len(clip_b64),
                max_bytes=max_bytes,
            )
            return
        clip_bytes = base64.b64decode(clip_b64)
        if len(clip_bytes) > max_bytes:
            log.error(
                "threshold_handler.clip_too_large",
                camera_path=cam_path,
                person_id=person_id,
                decoded_bytes=len(clip_bytes),
                max_bytes=max_bytes,
            )
            return
        declared_size = int(data["file_size_bytes"])
        if len(clip_bytes) != declared_size:
            log.error(
                "threshold_handler.clip_size_mismatch",
                camera_path=cam_path,
                person_id=person_id,
                decoded_bytes=len(clip_bytes),
                declared_bytes=declared_size,
            )
            return
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
                file_size_bytes=declared_size,
                sha256=sha_hex,
            )
            db.add(clip)
            await db.flush()
            clip_id = clip.id

        # 3. (verdict + level already parsed in step 1b; non-ignore reaches here)

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
                triggered_behaviors=behaviors or None,
                triggered_sequences=sequences or None,
                triggered_behavior_detail=behavior_detail or None,
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
            # Mark the person's episode as alerted ONLY now that the alert truly
            # exists — so the episode summary's "сэрэмжлүүлэг үүссэн" is honest.
            async with self._lock:
                st = self._state.get((cam_path, person_id))
                if st is not None:
                    st.alerted = True


# === Helpers ===


def build_behavior_detail(
    behaviors: list[str],
    scores: dict[str, float],
    offsets: dict[str, float],
) -> list[dict[str, Any]]:
    """Per-criterion timeline rows for the alert detail, in first-fired order.

    Each row = {key, offset_sec, score}. Keys follow the node's first-fired
    order (`behaviors`); score/offset are looked up from the banked ledgers
    (0.0 if a key is somehow absent). Empty list when nothing fired.
    """
    return [
        {
            "key": key,
            "offset_sec": round(float(offsets.get(key, 0.0)), 1),
            "score": round(float(scores.get(key, 0.0)), 1),
        }
        for key in behaviors
    ]


def compute_clip_window(
    *,
    breach_ts_ms: int | None,
    episode_started_ms: int | None,
    pre_pad_sec: int,
    post_roll_sec: float,
    max_sec: int,
) -> tuple[int, int]:
    """Size the breach clip so it covers the WHOLE episode.

    The cut runs on the AI node ~post_roll_sec after the breach (we sleep that
    long first), so node-"now" at cut time ≈ breach + post_roll. The window we
    request ends at that "now" and reaches back to the episode's first fired
    criterion minus pre_pad. Returns (start_offset_sec, duration_sec) for
    CutVerifyRequest. breach_ts_ms and episode_started_ms are both NODE clocks
    (frame ts_ms / TrackPayload.episode_started_ms), so backend↔node skew
    cancels out. Falls back to the legacy 5s pre-roll when episode start is
    unknown. Capped at max_sec by trimming the OLD end — the breach moment and
    post-roll always survive.
    """
    if (
        episode_started_ms is not None
        and breach_ts_ms is not None
        and episode_started_ms <= breach_ts_ms
    ):
        pre_sec = (breach_ts_ms - episode_started_ms) / 1000.0 + pre_pad_sec
    else:
        pre_sec = 5.0  # legacy fixed pre-roll
    # +2s slack absorbs HTTP/queue latency between our sleep ending and the
    # node stamping its own "now" — without it the episode start can fall just
    # outside the window.
    duration = math.ceil(pre_sec + post_roll_sec + 2.0)
    duration = max(5, min(max_sec, duration))
    return -duration, duration


async def _resolve_camera_by_mediamtx_path(path: str) -> Camera | None:
    async with session_scope() as db:
        result = await db.execute(select(Camera).where(Camera.mediamtx_path == path))
        return result.scalar_one_or_none()


_LIVE_ALERT_SEVERITY = {
    AlertLevel.ignore: EventSeverity.info,
    AlertLevel.log: EventSeverity.info,
    AlertLevel.notify: EventSeverity.warning,
    AlertLevel.review: EventSeverity.critical,
}


async def create_live_alert(
    *,
    mediamtx_path: str,
    clip_bytes: bytes,
    category: AlertCategory,
    actions: list[str] | None = None,
    confidence: float,
    reasoning: str,
    model_name: str,
    inference_latency_ms: int,
    duration_sec_clip: float,
    captured_at: datetime,
    file_size_bytes: int,
    embedding: list[float] | None = None,
    person_id: int | None = None,
    peak_risk_pct: float | None = None,
    behaviors: list[str] | None = None,
    sequences: list[str] | None = None,
    behavior_detail: list[dict[str, Any]] | None = None,
    pose_sequence: list[dict[str, Any]] | None = None,
) -> AlertPublic | None:
    """Node-push path: the AI node detected the breach, cut + VLM-verified the
    clip LOCALLY, and POSTed the finished result here (outbound = reliable,
    unlike the old cloud→node pull). We persist the clip, create the
    live_threshold Alert, notify, publish SSE, and log. None if the camera is
    unknown or the clip is rejected. The node has already applied the VLM gate
    (it never pushes an "ignore" verdict)."""
    settings = get_settings()
    camera = await _resolve_camera_by_mediamtx_path(mediamtx_path)
    if camera is None:
        log.warning("live_alert.camera_unknown", mediamtx_path=mediamtx_path)
        return None

    level = derive_alert_level(category, confidence)
    if level == AlertLevel.ignore:
        log.info(
            "live_alert.vlm_cleared",
            mediamtx_path=mediamtx_path,
            person_id=person_id,
            category=category.value,
            confidence=confidence,
        )
        return None

    # Debounce: the VLM-primary scan fires every few seconds, so without this the
    # menu floods with repeat "browsing" rows. Suppress a new alert only if a
    # SAME-OR-HIGHER severity alert for this (camera, person) landed within the
    # cooldown — an ESCALATION (e.g. browsing→theft) still gets through.
    if person_id is not None:
        async with session_scope() as db:
            if await alert_repo.recent_live_alert_exists(
                db,
                camera_id=camera.id,
                person_id=person_id,
                within_sec=settings.live_breach_cooldown_sec,
                at_or_above=level,
            ):
                log.info(
                    "live_alert.debounced",
                    mediamtx_path=mediamtx_path,
                    person_id=person_id,
                    level=level.value,
                )
                return None

    max_bytes = settings.max_clip_size_mb * 1024 * 1024
    if len(clip_bytes) > max_bytes:
        log.error("live_alert.clip_too_large", decoded_bytes=len(clip_bytes), max_bytes=max_bytes)
        return None
    out_dir = Path(settings.clip_storage_dir).resolve()  # noqa: ASYNC240
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    out_path = out_dir / f"live_{uuid4().hex}.mp4"
    out_path.write_bytes(clip_bytes)  # noqa: ASYNC230
    sha_hex = hashlib.sha256(clip_bytes).hexdigest()

    alert_public: AlertPublic | None = None
    async with session_scope() as db:
        cam_loaded = await db.get(Camera, camera.id)
        if cam_loaded is None:
            return None
        store = await db.get(Store, cam_loaded.store_id)
        if store is None:
            return None
        org_id = store.organization_id
        clip = Clip(
            organization_id=org_id,
            store_id=cam_loaded.store_id,
            camera_id=cam_loaded.id,
            captured_at=captured_at,
            duration_sec=duration_sec_clip,
            storage_path=out_path.as_posix(),
            file_size_bytes=file_size_bytes,
            sha256=sha_hex,
        )
        db.add(clip)
        await db.flush()
        # `level` was computed up front (used for the debounce check).
        alert = await alert_repo.create_alert(
            db,
            clip_id=clip.id,
            organization_id=org_id,
            store_id=cam_loaded.store_id,
            camera_id=cam_loaded.id,
            category=category,
            # Fall back to the single primary for older nodes that send no list.
            actions=actions or [category.value],
            confidence=confidence,
            reasoning=reasoning,
            model_name=model_name,
            alert_level=level,
            inference_latency_ms=inference_latency_ms,
            triggered_by=AlertTrigger.live_threshold,
            person_id=person_id,
            peak_risk_pct=peak_risk_pct,
            triggered_behaviors=behaviors or None,
            triggered_sequences=sequences or None,
            triggered_behavior_detail=behavior_detail or None,
            embedding=embedding,
            pose_sequence=pose_sequence or None,
        )
        alert_public = AlertPublic.model_validate(alert)
        await alert_notify.notify_alert(db, alert)
        await event_log.emit(
            db,
            event_type=EventType.alert_created,
            severity=_LIVE_ALERT_SEVERITY.get(level, EventSeverity.warning),
            message=f"Сэжигтэй үйлдэл илрлээ: {category.value} ({confidence:.0%})",
            organization_id=org_id,
            store_id=cam_loaded.store_id,
            camera_id=cam_loaded.id,
            actor_label="AI",
            detail={
                "alert_id": str(alert.id),
                "category": category.value,
                "confidence": confidence,
                "alert_level": level.value,
                "model_name": model_name,
                "person_id": person_id,
                "peak_risk_pct": peak_risk_pct,
            },
        )

    if alert_public is not None:
        await get_broker().publish(
            alert_public.organization_id, alert_public.model_dump(mode="json")
        )
        log.info(
            "live_alert.created",
            alert_id=str(alert_public.id),
            mediamtx_path=mediamtx_path,
            person_id=person_id,
        )
    return alert_public


_BREACH_CLEARED_REASONS = {
    "vlm_browsing": "VLM энгийн худалдан авалт гэж үзсэн",
    "vlm_low_confidence": "VLM-ийн итгэл хүрэлцээгүй",
    "cut_failed": "Бичлэг огтлох амжилтгүй",
}


async def record_breach_cleared(
    *,
    mediamtx_path: str,
    reason: str,
    peak_risk_pct: float | None = None,
    person_id: int | None = None,
    category: str | None = None,
    confidence: float | None = None,
    behaviors: list[str] | None = None,
) -> None:
    """Observability: the node detected a sustained breach but it did NOT become
    an alert (VLM cleared it, or the clip-cut failed). Write a risk_episode row
    so the operator SEES the miss on the activity timeline instead of silence —
    this makes the detection funnel (breach → cleared/alerted) visible + tunable.
    Best-effort; never raises."""
    try:
        camera = await _resolve_camera_by_mediamtx_path(mediamtx_path)
        if camera is None:
            return
        pct = peak_risk_pct or 0.0
        level = _risk_level(pct)
        severity = EventSeverity.warning if level in ("HIGH", "CRITICAL") else EventSeverity.info
        reason_text = _BREACH_CLEARED_REASONS.get(reason, reason)
        conf_txt = f" ({confidence:.0%})" if confidence is not None else ""
        async with session_scope() as db:
            store = await db.get(Store, camera.store_id) if camera.store_id else None
            org_id = store.organization_id if store else None
            await event_log.emit(
                db,
                event_type=EventType.risk_episode,
                severity=severity,
                message=(
                    f"Объект {camera.name}: эрсдэл {pct:.0f}% илэрсэн ч "
                    f"сэрэмжлүүлэг үүсээгүй — {reason_text}{conf_txt}"
                ),
                organization_id=org_id,
                store_id=camera.store_id,
                camera_id=camera.id,
                actor_label="AI",
                detail={
                    "reason": reason,
                    "person_id": person_id,
                    "peak_risk_pct": round(pct, 1),
                    "level": level,
                    "category": category,
                    "confidence": confidence,
                    "behaviors": behaviors,
                    "alerted": False,
                },
            )
    except Exception:  # noqa: BLE001 — diagnostics must never disrupt the node path
        log.warning("breach_cleared.emit_failed", exc_info=True)


# === Singleton ===

_handler: ThresholdHandler | None = None


def get_threshold_handler() -> ThresholdHandler:
    global _handler
    if _handler is None:
        _handler = ThresholdHandler()
    return _handler
