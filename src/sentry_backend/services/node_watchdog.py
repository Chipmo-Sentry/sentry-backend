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
from sentry_backend.settings import get_settings

log = get_logger("sentry_backend.node_watchdog")


class NodeLike(Protocol):
    """The minimal node shape the pure detection logic needs (testable w/o DB)."""

    id: UUID
    name: str | None
    hostname: str | None
    last_seen_at: datetime | None
    created_at: datetime


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
        for node in recovered:
            log.info("ai_node_recovered", node_id=str(node.id), name=_node_label(node))
            await _send_telegram(_format_recovered(node))
