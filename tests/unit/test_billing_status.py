"""T14 billing_status — the single gating truth (pure)."""

from datetime import UTC, datetime, timedelta

from sentry_backend.services.billing.status import BillingStatus, billing_status

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(days=3)
PAST = NOW - timedelta(days=3)


def test_positive_balance_is_active() -> None:
    st = billing_status(balance_mnt=1, credit_until=None, promo_free_until=None, now=NOW)
    assert st is BillingStatus.active


def test_zero_balance_no_credit_no_promo_is_suspended() -> None:
    st = billing_status(balance_mnt=0, credit_until=None, promo_free_until=None, now=NOW)
    assert st is BillingStatus.suspended


def test_negative_balance_is_suspended() -> None:
    st = billing_status(balance_mnt=-500, credit_until=None, promo_free_until=None, now=NOW)
    assert st is BillingStatus.suspended


def test_promo_window_overrides_empty_balance() -> None:
    st = billing_status(balance_mnt=0, credit_until=None, promo_free_until=FUTURE, now=NOW)
    assert st is BillingStatus.active


def test_expired_promo_does_not_help() -> None:
    st = billing_status(balance_mnt=0, credit_until=None, promo_free_until=PAST, now=NOW)
    assert st is BillingStatus.suspended


def test_credit_keeps_empty_wallet_alive() -> None:
    st = billing_status(balance_mnt=0, credit_until=FUTURE, promo_free_until=None, now=NOW)
    assert st is BillingStatus.credit


def test_expired_credit_is_suspended() -> None:
    st = billing_status(balance_mnt=-100, credit_until=PAST, promo_free_until=None, now=NOW)
    assert st is BillingStatus.suspended


def test_positive_balance_beats_credit_label() -> None:
    # With money in the wallet the org is plain `active`, even if credit is set.
    st = billing_status(balance_mnt=5_000, credit_until=FUTURE, promo_free_until=None, now=NOW)
    assert st is BillingStatus.active


def test_promo_beats_credit_label() -> None:
    st = billing_status(balance_mnt=-1, credit_until=FUTURE, promo_free_until=FUTURE, now=NOW)
    assert st is BillingStatus.active


def test_boundary_credit_exactly_now_is_expired() -> None:
    st = billing_status(balance_mnt=0, credit_until=NOW, promo_free_until=None, now=NOW)
    assert st is BillingStatus.suspended
