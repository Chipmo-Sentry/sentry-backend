"""Visitor number «YYYYMMDD-NNN» on analytics paths (owner request 09-03)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sentry_backend.repository import analytics_repo


def test_local_day_uses_store_timezone() -> None:
    # 23:30 UTC on the 2nd is already the 3rd in Ulaanbaatar (UTC+8).
    ts = datetime(2026, 9, 2, 23, 30, tzinfo=UTC)
    assert analytics_repo._local_day(ts, "Asia/Ulaanbaatar") == "20260903"
    assert analytics_repo._local_day(ts, "UTC") == "20260902"


def test_local_day_bad_timezone_falls_back_to_utc() -> None:
    ts = datetime(2026, 9, 2, 23, 30, tzinfo=UTC)
    assert analytics_repo._local_day(ts, "Mars/Olympus") == "20260902"


def test_local_day_naive_timestamp_is_utc() -> None:
    ts = datetime(2026, 9, 2, 23, 30)  # noqa: DTZ001 — the naive case under test
    assert analytics_repo._local_day(ts, "Asia/Ulaanbaatar") == "20260903"


@pytest.mark.asyncio
async def test_next_visitor_seq_continues_from_last() -> None:
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value="20260903-041")))
    assert await analytics_repo._next_visitor_seq(db, uuid4(), "20260903") == 42


@pytest.mark.asyncio
async def test_next_visitor_seq_starts_at_one() -> None:
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=None)))
    assert await analytics_repo._next_visitor_seq(db, uuid4(), "20260903") == 1


@pytest.mark.asyncio
async def test_insert_paths_numbers_rows_per_local_day_in_arrival_order() -> None:
    """Two visits on the 3rd (UA time) and one that is still the 2nd: the
    numbers continue each day's own sequence and follow started_at, not the
    order the rows arrived in."""
    store_id = uuid4()
    executed: list[object] = []

    async def execute(stmt: object) -> MagicMock:
        executed.append(stmt)
        text = str(stmt)
        res = MagicMock()
        if "stores.timezone" in text:
            res.scalar = MagicMock(return_value="Asia/Ulaanbaatar")
        elif "visitor_id LIKE" in text:
            # The 3rd already has 5 visits stored; the 2nd has none.
            like = str(stmt.compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
            res.scalar = MagicMock(return_value="20260903-005" if "20260903-%" in like else None)
        else:
            res.scalar = MagicMock(return_value=None)
        return res

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    rows = [
        (
            "cam",
            datetime(2026, 9, 3, 2, 0, tzinfo=UTC),
            30.0,
            [[0.1, 0.1]],
            None,
            None,
        ),  # 10:00 UA, 3rd
        (
            "cam",
            datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
            20.0,
            [[0.2, 0.2]],
            None,
            None,
        ),  # 04:00 UA, 3rd (earlier)
        (
            "cam",
            datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
            10.0,
            [[0.3, 0.3]],
            "male",
            None,
        ),  # 18:00 UA, 2nd
    ]
    await analytics_repo.insert_paths(db, organization_id=uuid4(), store_id=store_id, rows=rows)

    insert = executed[-1]
    values = insert.compile().params  # type: ignore[attr-defined]
    got = sorted(
        (v for k, v in values.items() if k.startswith("visitor_id")),
    )
    assert got == ["20260902-001", "20260903-006", "20260903-007"]
    # Arrival order within the 3rd: 04:00 UA gets 006, 10:00 UA gets 007.
    by_started = {
        values[k.replace("visitor_id", "started_at")]: v
        for k, v in values.items()
        if k.startswith("visitor_id")
    }
    assert by_started[datetime(2026, 9, 2, 20, 0, tzinfo=UTC)] == "20260903-006"
    assert by_started[datetime(2026, 9, 3, 2, 0, tzinfo=UTC)] == "20260903-007"
