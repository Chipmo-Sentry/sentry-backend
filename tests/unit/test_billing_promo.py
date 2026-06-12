"""T14 promo — redemption rules + window stacking (pure)."""

from datetime import UTC, datetime, timedelta

from sentry_backend.db.models.billing import PromoCode, PromoKind
from sentry_backend.services.billing.promo import (
    ERR_ALREADY_REDEEMED,
    ERR_EXHAUSTED,
    ERR_NOT_FOUND,
    extend_promo_window,
    generate_code,
    normalize_code,
    validate_promo,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


def _promo(**overrides: object) -> PromoCode:
    base: dict[str, object] = {
        "code": "WELCOME1",
        "kind": PromoKind.bonus_amount,
        "amount_mnt": 50_000,
        "valid_until": None,
        "max_redemptions": 1,
        "redeemed_count": 0,
        "active": True,
    }
    base.update(overrides)
    return PromoCode(**base)  # type: ignore[arg-type]


# --- validate_promo -----------------------------------------------------------


def test_missing_code_rejected() -> None:
    assert validate_promo(None, already_redeemed=False, now=NOW) == ERR_NOT_FOUND


def test_inactive_code_reported_as_not_found() -> None:
    # No oracle for guessing which codes exist.
    promo = _promo(active=False)
    assert validate_promo(promo, already_redeemed=False, now=NOW) == ERR_NOT_FOUND


def test_expired_code_rejected() -> None:
    promo = _promo(valid_until=NOW - timedelta(days=1))
    assert validate_promo(promo, already_redeemed=False, now=NOW) == ERR_NOT_FOUND


def test_valid_until_exactly_now_is_expired() -> None:
    promo = _promo(valid_until=NOW)
    assert validate_promo(promo, already_redeemed=False, now=NOW) == ERR_NOT_FOUND


def test_already_redeemed_by_this_org() -> None:
    promo = _promo()
    assert validate_promo(promo, already_redeemed=True, now=NOW) == ERR_ALREADY_REDEEMED


def test_exhausted_code_rejected() -> None:
    promo = _promo(max_redemptions=3, redeemed_count=3)
    assert validate_promo(promo, already_redeemed=False, now=NOW) == ERR_EXHAUSTED


def test_valid_code_passes() -> None:
    promo = _promo(valid_until=NOW + timedelta(days=7), max_redemptions=5, redeemed_count=4)
    assert validate_promo(promo, already_redeemed=False, now=NOW) is None


# --- free-days window stacking -------------------------------------------------


def test_window_starts_at_now_when_none() -> None:
    assert extend_promo_window(None, free_days=14, now=NOW) == NOW + timedelta(days=14)


def test_window_starts_at_now_when_expired() -> None:
    expired = NOW - timedelta(days=2)
    assert extend_promo_window(expired, free_days=7, now=NOW) == NOW + timedelta(days=7)


def test_open_window_stacks() -> None:
    open_until = NOW + timedelta(days=3)
    assert extend_promo_window(open_until, free_days=7, now=NOW) == open_until + timedelta(days=7)


# --- code helpers --------------------------------------------------------------


def test_generate_code_shape() -> None:
    code = generate_code()
    assert len(code) == 8
    assert all(c.isascii() and (c.isdigit() or c.isupper()) for c in code)


def test_generated_codes_are_random() -> None:
    assert len({generate_code() for _ in range(20)}) > 1


def test_normalize_code_uppercases_and_strips() -> None:
    assert normalize_code("  welcome1 ") == "WELCOME1"
