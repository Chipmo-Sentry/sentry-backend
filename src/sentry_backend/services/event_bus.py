"""In-memory pub/sub for the activity/event log → SSE fanout.

Single-process broker (ADR-0025, one replica). Mirrors live_broker but routes
by tenancy instead of camera: a subscriber either watches ONE org (org-admin
Logs page) or watches EVERYTHING with org_id=None (super-admin Logs page).

Publish routing for an event with `organization_id = O`:
  - every platform subscriber (org_filter is None) receives it;
  - every org subscriber whose org_filter == O receives it.
Platform-level events (organization_id is None) reach only platform subscribers.

Each subscriber has a bounded drop-oldest queue — a slow browser loses old
rows rather than blocking the publisher.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID

_QUEUE_MAX = 200  # generous: events are far rarer than live frames


class _Sub:
    __slots__ = ("queue", "org_filter")

    def __init__(self, org_filter: UUID | None) -> None:
        # org_filter None → platform subscriber (super-admin), sees all events.
        self.org_filter = org_filter
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAX)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[_Sub] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self, org_filter: UUID | None) -> _Sub:
        sub = _Sub(org_filter)
        async with self._lock:
            self._subs.add(sub)
        return sub

    async def unsubscribe(self, sub: _Sub) -> None:
        async with self._lock:
            self._subs.discard(sub)

    async def publish(self, organization_id: UUID | None, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = [
                s
                for s in self._subs
                if s.org_filter is None
                or (organization_id is not None and s.org_filter == organization_id)
            ]
        for sub in targets:
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(payload)

    def subscriber_count(self) -> int:
        return len(self._subs)


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
