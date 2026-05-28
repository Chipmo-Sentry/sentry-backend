"""In-memory per-camera pub/sub for WebSocket live metadata fanout.

M1-LIVE Phase L3. Single-process broker keyed by camera_id (not org —
tenancy is a separate concern, see TODO M2 BE10 for multi-instance Redis
pub/sub).

Publishers: `POST /api/v1/internal/live-metadata` (called by sentry-ai).
Subscribers: `WebSocket /ws/live/{camera_id}` (called by browser).

Each subscriber has its own bounded queue with drop-oldest semantics —
slow clients lose old frames rather than blocking the publisher.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

_QUEUE_MAX = 30  # ~3 seconds @ 10 FPS — live > complete


class LiveBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, camera_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAX)
        async with self._lock:
            self._subscribers.setdefault(camera_id, set()).add(q)
        return q

    async def unsubscribe(self, camera_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            subs = self._subscribers.get(camera_id)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(camera_id, None)

    async def publish(self, camera_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(camera_id, set()))
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # drop-oldest on full
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(payload)

    def subscriber_count(self, camera_id: str | None = None) -> int:
        if camera_id is None:
            return sum(len(s) for s in self._subscribers.values())
        return len(self._subscribers.get(camera_id, set()))


_broker: LiveBroker | None = None


def get_live_broker() -> LiveBroker:
    global _broker
    if _broker is None:
        _broker = LiveBroker()
    return _broker
