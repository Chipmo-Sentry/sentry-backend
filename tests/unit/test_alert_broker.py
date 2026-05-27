"""Unit tests for AlertBroker pub/sub."""

import asyncio
from uuid import uuid4

import pytest

from sentry_backend.services.alert_broker import AlertBroker


@pytest.mark.asyncio
async def test_publish_to_single_subscriber() -> None:
    broker = AlertBroker()
    org = uuid4()
    q = await broker.subscribe(org)

    await broker.publish(org, {"alert_id": "abc"})

    item = await asyncio.wait_for(q.get(), timeout=1.0)
    assert item == {"alert_id": "abc"}


@pytest.mark.asyncio
async def test_publish_to_multiple_subscribers_same_org() -> None:
    broker = AlertBroker()
    org = uuid4()
    q1 = await broker.subscribe(org)
    q2 = await broker.subscribe(org)

    await broker.publish(org, {"x": 1})

    assert (await asyncio.wait_for(q1.get(), 1.0)) == {"x": 1}
    assert (await asyncio.wait_for(q2.get(), 1.0)) == {"x": 1}


@pytest.mark.asyncio
async def test_publish_isolates_orgs() -> None:
    broker = AlertBroker()
    org_a, org_b = uuid4(), uuid4()
    qa = await broker.subscribe(org_a)
    qb = await broker.subscribe(org_b)

    await broker.publish(org_a, {"for": "a"})

    assert (await asyncio.wait_for(qa.get(), 1.0)) == {"for": "a"}
    assert qb.empty()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    broker = AlertBroker()
    org = uuid4()
    q = await broker.subscribe(org)
    await broker.unsubscribe(org, q)

    await broker.publish(org, {"x": 1})
    assert q.empty()


@pytest.mark.asyncio
async def test_full_queue_drops_oldest_not_publisher() -> None:
    broker = AlertBroker()
    org = uuid4()
    q = await broker.subscribe(org)

    # Fill the queue (maxsize=100 per impl)
    for i in range(105):
        await broker.publish(org, {"i": i})

    # Publisher never blocked; queue size never exceeds max.
    assert q.qsize() <= 100
    # We dropped oldest, so newest payload should be present
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert {"i": 104} in drained
