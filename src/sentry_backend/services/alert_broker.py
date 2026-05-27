"""In-memory per-org pub/sub for SSE alert push.

M1: single-process broker. M2+ (multi-instance backend) needs Redis pub/sub or
similar — at that point the API is unchanged, only `_AlertBroker` swaps.
"""

import asyncio
import contextlib
from typing import Any
from uuid import UUID

_QUEUE_MAX = 100  # per-subscriber buffer; drop-oldest semantics


class AlertBroker:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, org_id: UUID) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAX)
        async with self._lock:
            self._subscribers.setdefault(org_id, set()).add(q)
        return q

    async def unsubscribe(self, org_id: UUID, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            subs = self._subscribers.get(org_id)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(org_id, None)

    async def publish(self, org_id: UUID, payload: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(org_id, set()))
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest then push (back-pressure: slow clients lose old events,
                # never block the publisher).
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(payload)


_broker: AlertBroker | None = None


def get_broker() -> AlertBroker:
    global _broker
    if _broker is None:
        _broker = AlertBroker()
    return _broker
