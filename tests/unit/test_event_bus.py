"""Unit tests for EventBus pub/sub tenancy routing (event-log SSE fanout)."""

import asyncio
from uuid import uuid4

import pytest

from sentry_backend.services.event_bus import EventBus


@pytest.mark.asyncio
async def test_org_subscriber_gets_only_its_org() -> None:
    bus = EventBus()
    org_a, org_b = uuid4(), uuid4()
    sa = await bus.subscribe(org_a)
    sb = await bus.subscribe(org_b)

    await bus.publish(org_a, {"for": "a"})

    assert (await asyncio.wait_for(sa.queue.get(), 1.0)) == {"for": "a"}
    assert sb.queue.empty()


@pytest.mark.asyncio
async def test_platform_subscriber_gets_everything() -> None:
    """A super-admin (org_filter=None) receives every org's events AND
    platform-level (org=None) events."""
    bus = EventBus()
    org = uuid4()
    platform = await bus.subscribe(None)

    await bus.publish(org, {"scope": "org"})
    await bus.publish(None, {"scope": "platform"})

    got = [
        await asyncio.wait_for(platform.queue.get(), 1.0),
        await asyncio.wait_for(platform.queue.get(), 1.0),
    ]
    assert {"scope": "org"} in got
    assert {"scope": "platform"} in got


@pytest.mark.asyncio
async def test_org_subscriber_never_sees_platform_events() -> None:
    """Platform-level (org=None) events are super-admin only — an org admin
    must not receive them."""
    bus = EventBus()
    org = uuid4()
    sub = await bus.subscribe(org)

    await bus.publish(None, {"scope": "platform"})

    assert sub.queue.empty()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    org = uuid4()
    sub = await bus.subscribe(org)
    await bus.unsubscribe(sub)

    await bus.publish(org, {"x": 1})
    assert sub.queue.empty()


@pytest.mark.asyncio
async def test_full_queue_drops_oldest_not_publisher() -> None:
    bus = EventBus()
    org = uuid4()
    sub = await bus.subscribe(org)

    for i in range(210):
        await bus.publish(org, {"i": i})

    assert sub.queue.qsize() <= 200  # never exceeds max; publisher never blocks
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    assert {"i": 209} in drained  # newest survives, oldest dropped
