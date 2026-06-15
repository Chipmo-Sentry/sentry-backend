"""AI node offline watchdog (T12 — active alerting).

Heartbeats only *arrive*; nothing ever noticed when they *stopped*. The admin
dashboard derives "online" at query time (last_seen_at within 3 minutes), so an
AI node could die at midnight and nobody learns until someone opens the
dashboard. This background loop closes that gap: every `node_watchdog_interval_sec`
it checks each active node's `last_seen_at` and pings the founder's Telegram
chat (same bot/chat as lead notifications) when a node goes offline — and again
when it comes back.

Duplicate-suppression state is in-memory (node_id → already-notified), which is
sufficient on a single replica (ADR-0025). A restart simply re-evaluates: a node
that is *still* offline after a backend restart will be re-notified once, which
is acceptable (arguably desirable).

Best-effort like lead_notify: missing Telegram config → transitions are only
logged; any error inside the loop is logged and the loop keeps running.
"""

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy import select

from sentry_backend.logging_setup import get_logger
from sentry_backend.schemas.ai_node import CameraHealth, parse_camera_health
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.node_watchdog")


class NodeLike(Protocol):
    """The minimal node shape the pure detection logic needs (testable w/o DB)."""

    id: UUID
    name: str | None
    hostname: str | None
    last_seen_at: datetime | None
    created_at: datetime
    telemetry: str | None


def is_offline(node: NodeLike, *, now: datetime, offline_after_sec: int) -> bool:
    """Offline = no heartbeat for `offline_after_sec`.

    A node that never heartbeated falls back to `created_at` (paired but silent
    for the threshold → offline), matching admin.py's "last_seen_at >= cutoff"
    counting where NULL is offline.
    """
    last = node.last_seen_at or node.created_at
    return (now - last).total_seconds() >= offline_after_sec


def detect_transitions(
    nodes: Sequence[NodeLike],
    notified_offline: set[UUID],
    *,
    now: datetime,
    offline_after_sec: int,
) -> tuple[list[NodeLike], list[NodeLike]]:
    """Pure state-machine step: returns (newly_offline, recovered).

    Mutates `notified_offline` to reflect the new state, so calling this again
    with unchanged nodes yields no transitions (one notification per outage).
    Nodes that vanished from the active list are pruned from the state set.
    """
    newly_offline: list[NodeLike] = []
    recovered: list[NodeLike] = []
    seen_ids: set[UUID] = set()

    for node in nodes:
        seen_ids.add(node.id)
        offline = is_offline(node, now=now, offline_after_sec=offline_after_sec)
        if offline and node.id not in notified_offline:
            notified_offline.add(node.id)
            newly_offline.append(node)
        elif not offline and node.id in notified_offline:
            notified_offline.discard(node.id)
            recovered.append(node)

    # Deactivated/deleted nodes must not leave stale "notified" entries behind
    # (they'd suppress the offline ping if the node is later re-activated).
    notified_offline.intersection_update(seen_ids)

    return newly_offline, recovered


# ── Per-camera stream health (audit T12 #3) ────────────────────────────────
# A camera key is (node_id, camera_id) — camera ids are only unique per node.
CameraKey = tuple[UUID, str]


def _camera_down(cam: CameraHealth) -> bool:
    """Down = the node says error/stalled, or it reports 0 FPS while claiming
    "ok" (defensive — current nodes never send that combination)."""
    return cam.status != "ok" or cam.fps == 0


def detect_camera_transitions(
    nodes: Sequence[NodeLike],
    candidate_down: set[CameraKey],
    notified_down: set[CameraKey],
    *,
    now: datetime,
    offline_after_sec: int,
) -> tuple[list[tuple[NodeLike, CameraHealth]], list[tuple[NodeLike, CameraHealth]]]:
    """Pure state-machine step for per-camera health: (newly_down, recovered).

    Debounced: a camera must be down on TWO consecutive checks before it
    notifies (one watchdog tick ≈ 60s), so a camera that reports "stalled" for
    a single beat while its worker warms up YOLO doesn't false-alarm.

    Offline nodes are skipped entirely — their telemetry is stale and the node
    watchdog already alerted; alerting every camera too would just be noise.
    Their camera state is kept as-is so recoveries still notify when the node
    returns. Cameras that vanish from telemetry (worker stopped on purpose)
    are pruned without a notification, mirroring detect_transitions.
    """
    newly_down: list[tuple[NodeLike, CameraHealth]] = []
    recovered: list[tuple[NodeLike, CameraHealth]] = []
    seen: set[CameraKey] = set()

    for node in nodes:
        if is_offline(node, now=now, offline_after_sec=offline_after_sec):
            # Freeze this node's camera state until it comes back online.
            seen.update(k for k in candidate_down | notified_down if k[0] == node.id)
            continue
        for cam in parse_camera_health(node.telemetry) or []:
            key: CameraKey = (node.id, cam.camera_id)
            seen.add(key)
            if _camera_down(cam):
                if key in notified_down:
                    continue  # already alerted this outage
                if key in candidate_down:
                    candidate_down.discard(key)
                    notified_down.add(key)
                    newly_down.append((node, cam))
                else:
                    candidate_down.add(key)  # first sighting — wait one tick
            else:
                candidate_down.discard(key)
                if key in notified_down:
                    notified_down.discard(key)
                    recovered.append((node, cam))

    candidate_down.intersection_update(seen)
    notified_down.intersection_update(seen)
    return newly_down, recovered


def _node_label(node: NodeLike) -> str:
    return node.name or node.hostname or str(node.id)


def _format_offline(node: NodeLike, *, now: datetime) -> str:
    last = node.last_seen_at or node.created_at
    ago_min = max(0, int((now - last).total_seconds() // 60))
    lines = [
        f"🔴 AI node офлайн боллоо — {_node_label(node)}",
        f"🕐 Сүүлд харагдсан: {ago_min} мин өмнө ({last.strftime('%Y-%m-%d %H:%M')} UTC)",
    ]
    if node.hostname and node.name:
        lines.insert(1, f"🖥 {node.hostname}")
    return "\n".join(lines)


def _format_recovered(node: NodeLike) -> str:
    return f"🟢 AI node буцаж онлайн боллоо — {_node_label(node)}"


def _format_camera_down(node: NodeLike, cam: CameraHealth) -> str:
    fps_txt = "?" if cam.fps is None else f"{cam.fps:g}"
    return (
        f"🟡 Камер унасан: {cam.camera_id} (node {_node_label(node)})\n"
        f"⚠️ Төлөв: {cam.status}, FPS: {fps_txt}"
    )


def _format_camera_recovered(node: NodeLike, cam: CameraHealth) -> str:
    return f"🟢 Камер сэргэлээ: {cam.camera_id} (node {_node_label(node)})"


async def _send_telegram(text: str) -> None:
    """Founder ping via the same bot/chat as lead_notify. Swallows all errors."""
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        log.info("node_watchdog_no_notifier", text=text)
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — notification must never kill the watchdog
        log.warning("node_watchdog_notify_failed", exc_info=True)


class NodeWatchdog:
    """Background loop started/stopped from the app lifespan."""

    def __init__(self) -> None:
        self._notified_offline: set[UUID] = set()
        # Per-camera health state (audit T12 #3): candidates seen down once
        # (debounce) and cameras already alerted this outage.
        self._camera_candidates: set[CameraKey] = set()
        self._notified_cameras: set[CameraKey] = set()
        # Desktop-agent (agent-pc) offline transitions for the event log.
        self._notified_agents_offline: set[UUID] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        settings = get_settings()
        if settings.node_watchdog_interval_sec <= 0:
            log.info("node_watchdog_disabled")
            return
        self._task = asyncio.create_task(self._run(), name="node-watchdog")
        log.info(
            "node_watchdog_started",
            interval_sec=settings.node_watchdog_interval_sec,
            offline_after_sec=settings.node_offline_after_sec,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        interval = get_settings().node_watchdog_interval_sec
        while True:
            try:
                await self._check_once()
            except Exception:  # noqa: BLE001 — the loop must outlive any error
                log.warning("node_watchdog_check_failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _check_once(self) -> None:
        # Imported here so the pure logic above stays importable without a DB
        # (unit tests exercise detect_transitions/_format directly).
        from sentry_backend.db.models.ai_node import AiNode
        from sentry_backend.db.session import session_scope

        settings = get_settings()
        now = datetime.now(UTC)
        async with session_scope() as db:
            nodes = list(
                (await db.execute(select(AiNode).where(AiNode.is_active.is_(True)))).scalars().all()
            )

        newly_offline, recovered = detect_transitions(
            nodes,
            self._notified_offline,
            now=now,
            offline_after_sec=settings.node_offline_after_sec,
        )
        for node in newly_offline:
            log.warning("ai_node_offline", node_id=str(node.id), name=_node_label(node))
            await _send_telegram(_format_offline(node, now=now))
            await self._emit_node(node, online=False)
        for node in recovered:
            log.info("ai_node_recovered", node_id=str(node.id), name=_node_label(node))
            await _send_telegram(_format_recovered(node))
            await self._emit_node(node, online=True)

        # Per-camera stream health from the latest telemetry (audit T12 #3).
        cams_down, cams_recovered = detect_camera_transitions(
            nodes,
            self._camera_candidates,
            self._notified_cameras,
            now=now,
            offline_after_sec=settings.node_offline_after_sec,
        )
        for node, cam in cams_down:
            log.warning(
                "camera_stream_down",
                node_id=str(node.id),
                camera_id=cam.camera_id,
                status=cam.status,
                fps=cam.fps,
            )
            await _send_telegram(_format_camera_down(node, cam))
            await self._emit_camera(node, cam, down=True)
        for node, cam in cams_recovered:
            log.info("camera_stream_recovered", node_id=str(node.id), camera_id=cam.camera_id)
            await _send_telegram(_format_camera_recovered(node, cam))
            await self._emit_camera(node, cam, down=False)

        # Desktop agents (agent-pc) — same offline/online state machine, event
        # log only (Telegram noise is reserved for AI nodes + cameras).
        await self._check_agents(now=now, offline_after_sec=settings.node_offline_after_sec)

    async def _check_agents(self, *, now: datetime, offline_after_sec: int) -> None:
        from sentry_backend.db.models.agent import Agent
        from sentry_backend.db.models.event_log import EventSeverity, EventType
        from sentry_backend.db.session import session_scope
        from sentry_backend.services import event_log

        try:
            async with session_scope() as db:
                agents = list(
                    (await db.execute(select(Agent).where(Agent.is_active.is_(True))))
                    .scalars()
                    .all()
                )
                newly_offline, recovered = detect_transitions(
                    agents,  # type: ignore[arg-type]  # Agent satisfies is_offline's needs
                    self._notified_agents_offline,
                    now=now,
                    offline_after_sec=offline_after_sec,
                )
                for agent in newly_offline:
                    log.warning("agent_offline", agent_id=str(agent.id))
                    await event_log.emit(
                        db,
                        event_type=EventType.agent_offline,
                        severity=EventSeverity.error,
                        message=f"Десктоп апп унтарлаа: {agent.name or 'нэргүй'}",
                        organization_id=agent.organization_id,  # type: ignore[attr-defined]
                        store_id=agent.store_id,  # type: ignore[attr-defined]
                        agent_id=agent.id,
                        actor_label="Систем",
                    )
                for agent in recovered:
                    log.info("agent_recovered", agent_id=str(agent.id))
                    await event_log.emit(
                        db,
                        event_type=EventType.agent_online,
                        severity=EventSeverity.success,
                        message=f"Десктоп апп буцаж асаалаа: {agent.name or 'нэргүй'}",
                        organization_id=agent.organization_id,  # type: ignore[attr-defined]
                        store_id=agent.store_id,  # type: ignore[attr-defined]
                        agent_id=agent.id,
                        actor_label="Систем",
                    )
                # Retention: drop heartbeat rows past the window (cheap indexed
                # delete; runs every watchdog tick).
                removed = await event_log.prune_heartbeats(
                    db, older_than_seconds=event_log.HEARTBEAT_RETENTION_SEC
                )
                if removed:
                    log.info("event_log_heartbeats_pruned", removed=removed)
        except Exception:  # noqa: BLE001 — watchdog must outlive logging errors
            log.warning("agent_watchdog_failed", exc_info=True)

    async def _emit_node(self, node: NodeLike, *, online: bool) -> None:
        """Platform-level event log row for an AI-node up/down transition."""
        from sentry_backend.db.models.event_log import EventSeverity, EventType
        from sentry_backend.db.session import session_scope
        from sentry_backend.services import event_log

        try:
            async with session_scope() as db:
                await event_log.emit(
                    db,
                    event_type=EventType.node_online if online else EventType.node_offline,
                    severity=EventSeverity.success if online else EventSeverity.error,
                    message=(
                        f"AI node буцаж онлайн боллоо: {_node_label(node)}"
                        if online
                        else f"AI node офлайн боллоо: {_node_label(node)}"
                    ),
                    ai_node_id=node.id,
                    actor_label="Систем",
                )
        except Exception:  # noqa: BLE001 — the watchdog must outlive logging errors
            log.warning("node_watchdog_event_emit_failed", exc_info=True)

    async def _emit_camera(self, node: NodeLike, cam: CameraHealth, *, down: bool) -> None:
        """Org-scoped event for a camera stream down/recovered. Resolves the
        owning org via Camera.mediamtx_path (the wire camera_id); platform-level
        if the camera can't be mapped."""
        from sqlalchemy import select

        from sentry_backend.db.models.camera import Camera
        from sentry_backend.db.models.event_log import EventSeverity, EventType
        from sentry_backend.db.models.store import Store
        from sentry_backend.db.session import session_scope
        from sentry_backend.services import event_log

        try:
            async with session_scope() as db:
                row = (
                    await db.execute(
                        select(Camera.id, Store.organization_id, Camera.name)
                        .join(Store, Camera.store_id == Store.id)
                        .where(Camera.mediamtx_path == cam.camera_id)
                    )
                ).first()
                camera_id = row[0] if row else None
                org_id = row[1] if row else None
                cam_name = row[2] if row else cam.camera_id
                await event_log.emit(
                    db,
                    event_type=EventType.camera_stream_down
                    if down
                    else EventType.camera_stream_recovered,
                    severity=EventSeverity.warning if down else EventSeverity.success,
                    message=(
                        f"Камер унаслаа: {cam_name}" if down else f"Камер сэргэлээ: {cam_name}"
                    ),
                    organization_id=org_id,
                    camera_id=camera_id,
                    ai_node_id=node.id,
                    actor_label="Систем",
                    detail={"status": cam.status, "fps": cam.fps},
                )
        except Exception:  # noqa: BLE001
            log.warning("node_watchdog_camera_event_emit_failed", exc_info=True)
