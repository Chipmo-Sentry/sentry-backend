"""Node watchdog — offline detection state machine + message formatting (no DB)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sentry_backend.services.node_watchdog import (
    _format_offline,
    _format_recovered,
    detect_transitions,
    is_offline,
)

NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
THRESHOLD = 180


@dataclass
class FakeNode:
    last_seen_at: datetime | None
    name: str | None = "Predator-ChipmoAI"
    hostname: str | None = "predator"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = NOW - timedelta(days=30)


def _node(seconds_ago: float | None) -> FakeNode:
    last = None if seconds_ago is None else NOW - timedelta(seconds=seconds_ago)
    return FakeNode(last_seen_at=last)


# --- is_offline ---------------------------------------------------------


def test_recent_heartbeat_is_online() -> None:
    assert not is_offline(_node(60), now=NOW, offline_after_sec=THRESHOLD)


def test_stale_heartbeat_is_offline() -> None:
    assert is_offline(_node(THRESHOLD + 1), now=NOW, offline_after_sec=THRESHOLD)


def test_never_heartbeated_falls_back_to_created_at() -> None:
    # Paired 30 days ago, never beat → offline (matches admin.py NULL handling).
    assert is_offline(_node(None), now=NOW, offline_after_sec=THRESHOLD)
    # Paired seconds ago, never beat yet → still within grace, online.
    fresh = FakeNode(last_seen_at=None, created_at=NOW - timedelta(seconds=10))
    assert not is_offline(fresh, now=NOW, offline_after_sec=THRESHOLD)


# --- detect_transitions (one notification per outage + recovery) ---------


def test_offline_node_notifies_once() -> None:
    node = _node(THRESHOLD + 60)
    state: set[UUID] = set()

    offline, recovered = detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)
    assert offline == [node]
    assert recovered == []
    assert node.id in state

    # Same node still offline on the next tick → no duplicate notification.
    offline, recovered = detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)
    assert offline == []
    assert recovered == []


def test_online_node_never_notifies() -> None:
    node = _node(30)
    state: set[UUID] = set()
    offline, recovered = detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)
    assert offline == []
    assert recovered == []
    assert state == set()


def test_recovery_notifies_and_resets_state() -> None:
    node = _node(THRESHOLD + 60)
    state: set[UUID] = set()
    detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)

    # Heartbeat resumes.
    node.last_seen_at = NOW
    offline, recovered = detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)
    assert offline == []
    assert recovered == [node]
    assert state == set()

    # And a later outage notifies again (full cycle).
    node.last_seen_at = NOW - timedelta(seconds=THRESHOLD + 5)
    offline, _ = detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)
    assert offline == [node]


def test_mixed_nodes_only_transition_the_right_ones() -> None:
    dead = _node(THRESHOLD * 10)
    alive = _node(5)
    state: set[UUID] = set()
    offline, recovered = detect_transitions(
        [dead, alive], state, now=NOW, offline_after_sec=THRESHOLD
    )
    assert offline == [dead]
    assert recovered == []
    assert state == {dead.id}


def test_vanished_node_is_pruned_from_state() -> None:
    node = _node(THRESHOLD + 60)
    state: set[UUID] = set()
    detect_transitions([node], state, now=NOW, offline_after_sec=THRESHOLD)
    assert state == {node.id}

    # Node deactivated (gone from the active list) → stale state pruned, so a
    # later re-activation while offline notifies again instead of being muted.
    offline, recovered = detect_transitions([], state, now=NOW, offline_after_sec=THRESHOLD)
    assert offline == []
    assert recovered == []
    assert state == set()


def test_threshold_boundary_is_offline() -> None:
    # Exactly at the threshold counts as offline (>=), matching the docstring.
    node = _node(THRESHOLD)
    assert is_offline(node, now=NOW, offline_after_sec=THRESHOLD)


# --- message formatting (Mongolian, name + last-seen) ---------------------


def test_offline_message_has_name_and_last_seen() -> None:
    node = _node(THRESHOLD + 120)  # 5 минут өмнө
    msg = _format_offline(node, now=NOW)
    assert "🔴" in msg
    assert "офлайн" in msg
    assert "Predator-ChipmoAI" in msg
    assert "5 мин өмнө" in msg
    assert "UTC" in msg


def test_offline_message_falls_back_to_hostname_then_id() -> None:
    node = _node(THRESHOLD + 60)
    node.name = None
    assert "predator" in _format_offline(node, now=NOW)
    node.hostname = None
    assert str(node.id) in _format_offline(node, now=NOW)


def test_recovered_message() -> None:
    node = _node(0)
    msg = _format_recovered(node)
    assert "🟢" in msg
    assert "онлайн" in msg
    assert "Predator-ChipmoAI" in msg
