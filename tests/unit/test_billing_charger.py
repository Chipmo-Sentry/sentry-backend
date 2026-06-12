"""T14 daily charger — UB calendar-day key + daily amount math (pure parts)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from sentry_backend.services.billing.charger import BillingCharger, ub_today
from sentry_backend.services.billing.pricing import org_daily_charge_mnt
from sentry_backend.settings import get_settings

# --- Ulaanbaatar day key (the idempotency key of the unique index) -----------


def test_ub_today_is_utc_plus_8() -> None:
    # 2026-06-12 17:30 UTC = 2026-06-13 01:30 in Ulaanbaatar (UTC+8).
    assert ub_today(datetime(2026, 6, 12, 17, 30, tzinfo=UTC)) == date(2026, 6, 13)


def test_ub_today_before_midnight_ub() -> None:
    # 2026-06-12 15:59 UTC = 2026-06-12 23:59 UB — still the 12th.
    assert ub_today(datetime(2026, 6, 12, 15, 59, tzinfo=UTC)) == date(2026, 6, 12)


def test_same_ub_day_means_same_charge_key() -> None:
    # Two sweeps 8 hours apart within one UB day → identical charge_date →
    # second insert hits the partial unique index → idempotent.
    first = datetime(2026, 6, 12, 1, 0, tzinfo=UTC)  # 09:00 UB
    second = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)  # 17:00 UB
    assert ub_today(first) == ub_today(second)


# --- the amount one day costs ---------------------------------------------------


def test_daily_amount_example_org() -> None:
    # Store A: 3 cams starter → 115,000/mo → 3,833/day (115000/30 = 3833.33)
    # Store B: 12 cams pro    → 243,000/mo → 8,100/day
    # Store C: empty          → free
    assert org_daily_charge_mnt([3, 12, 0]) == 3_833 + 8_100


# --- loop wiring ------------------------------------------------------------------


@pytest.fixture()
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


async def test_charger_disabled_when_interval_nonpositive(
    _settings_env: pytest.MonkeyPatch,
) -> None:
    _settings_env.setenv("BILLING_CHARGE_INTERVAL_SEC", "0")
    charger = BillingCharger()
    charger.start()
    assert charger._task is None  # noqa: SLF001 — asserting the loop never spawned
    await charger.stop()  # no-op, must not raise
