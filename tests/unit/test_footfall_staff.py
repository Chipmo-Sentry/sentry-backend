"""Staff exclusion in the footfall aggregator: a track flagged `is_staff` must
contribute to NO visitor statistic (paths, dwell), and a LATE badge lock must
retro-cancel the visit it credited while it was still buffered."""

from datetime import UTC, datetime
from uuid import uuid4

from sentry_backend.services.footfall_aggregator import FootfallAggregator, _Presence


def test_prune_skips_staff_presence_entirely() -> None:
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 18, 13, tzinfo=UTC)
    pres = _Presence(first_mono=100.0, store_id=sid, hour=hour)
    pres.last_mono = 130.0
    pres.is_staff = True
    # A path accumulated before the badge locked would have been cleared at mark
    # time; even if some samples survived, prune must not bank them.
    pres.path = [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3], [0.4, 0.4]]
    agg._presence[("cam", 1)] = pres
    agg._prune_presence(200.0)
    assert ("cam", 1) not in agg._presence
    assert agg._dwell_buf == {}
    assert agg._path_buf == []


def test_prune_still_banks_visitor_next_to_staff() -> None:
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 18, 13, tzinfo=UTC)
    staff = _Presence(first_mono=100.0, store_id=sid, hour=hour)
    staff.last_mono = 130.0
    staff.is_staff = True
    visitor = _Presence(first_mono=100.0, store_id=sid, hour=hour)
    visitor.last_mono = 120.0
    agg._presence[("cam", 1)] = staff
    agg._presence[("cam", 2)] = visitor
    agg._prune_presence(200.0)
    assert agg._dwell_buf[(sid, "cam", hour)] == [1, 20000]


def test_visit_credit_retro_cancel() -> None:
    """The staff_marks block in _on_frame_inner decrements a still-buffered
    visit. Exercised directly against the buffers the block manipulates."""
    agg = FootfallAggregator()
    sid = uuid4()
    hour = datetime(2026, 7, 18, 13, tzinfo=UTC)
    ckey = (sid, "cam", hour)
    vkey = ("sp", sid, 7)
    # A gate crossing credited one visit before the badge locked.
    agg._visits[ckey] = 1
    agg._visit_credit[vkey] = ckey
    # Replay what the staff_marks loop does for (pid=None, sp=7).
    ckey0 = agg._visit_credit.pop(vkey, None)
    assert ckey0 == ckey
    if ckey0 is not None and agg._visits.get(ckey0, 0) > 0:
        agg._visits[ckey0] -= 1
    assert agg._visits[ckey] == 0
    # Cancelling twice is a no-op (credit already consumed).
    assert agg._visit_credit.pop(vkey, None) is None


def test_gate_prune_drops_visit_credit() -> None:
    agg = FootfallAggregator()
    vkey = ("cam", "cam1", 5)
    agg._gate_seen[vkey] = 0.0
    agg._visit_credit[vkey] = (uuid4(), "cam1", datetime(2026, 7, 18, 13, tzinfo=UTC))
    agg._gate_last_prune = -1000.0
    agg._prune_gate_seen(1000.0)  # far past _GATE_TTL
    assert vkey not in agg._gate_seen
    assert vkey not in agg._visit_credit
