"""T14 ledger — Dr/Cr invariants per journal kind (pure, no DB)."""

import pytest

from sentry_backend.db.models.billing import JournalKind, LedgerAccount
from sentry_backend.services.billing.ledger import accounts_for, balance_delta

# --- fixed account mapping per kind ------------------------------------------


def test_topup_is_dr_cash_cr_wallet() -> None:
    assert accounts_for(JournalKind.topup) == (LedgerAccount.cash, LedgerAccount.org_wallet)


def test_usage_is_dr_wallet_cr_revenue() -> None:
    assert accounts_for(JournalKind.usage_charge) == (
        LedgerAccount.org_wallet,
        LedgerAccount.revenue,
    )


def test_promo_is_dr_promo_expense_cr_wallet() -> None:
    assert accounts_for(JournalKind.promo_credit) == (
        LedgerAccount.promo_expense,
        LedgerAccount.org_wallet,
    )


def test_adjustment_has_no_fixed_mapping() -> None:
    with pytest.raises(ValueError, match="adjustment"):
        accounts_for(JournalKind.adjustment)


# --- wallet delta -------------------------------------------------------------


def test_topup_increases_balance() -> None:
    dr, cr = accounts_for(JournalKind.topup)
    assert balance_delta(dr, cr, 50_000) == 50_000


def test_usage_decreases_balance() -> None:
    dr, cr = accounts_for(JournalKind.usage_charge)
    assert balance_delta(dr, cr, 2_367) == -2_367


def test_promo_credit_increases_balance() -> None:
    dr, cr = accounts_for(JournalKind.promo_credit)
    assert balance_delta(dr, cr, 10_000) == 10_000


def test_usage_plus_equal_promo_nets_zero() -> None:
    # The charger's promo-window behavior: charge + equal credit → net 0.
    usage = balance_delta(*accounts_for(JournalKind.usage_charge), 2_367)
    promo = balance_delta(*accounts_for(JournalKind.promo_credit), 2_367)
    assert usage + promo == 0


def test_adjustment_without_wallet_does_not_move_balance() -> None:
    assert balance_delta(LedgerAccount.cash, LedgerAccount.revenue, 9_999) == 0


def test_adjustment_cr_wallet_adds() -> None:
    assert balance_delta(LedgerAccount.cash, LedgerAccount.org_wallet, 1_000) == 1_000


def test_adjustment_dr_wallet_removes() -> None:
    assert balance_delta(LedgerAccount.org_wallet, LedgerAccount.cash, 1_000) == -1_000


def test_same_account_both_sides_is_neutral() -> None:
    assert balance_delta(LedgerAccount.org_wallet, LedgerAccount.org_wallet, 1_000) == 0
