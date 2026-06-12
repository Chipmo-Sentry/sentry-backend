"""Double-entry posting — journal row + wallet balance in ONE transaction.

Account semantics (Dr increases asset/expense, Cr increases liability/income;
org_wallet is OUR liability to the org — prepaid money we owe back in service):

* topup        — Dr cash         / Cr org_wallet   → balance += amount
* usage_charge — Dr org_wallet   / Cr revenue      → balance -= amount
* promo_credit — Dr promo_expense/ Cr org_wallet   → balance += amount
* adjustment   — accounts chosen by the operator; balance moves only when
                 org_wallet is on one side (Cr → +, Dr → -).

`accounts_for` / `balance_delta` are pure so the Dr/Cr invariant is unit-tested
without a database. `post_entry` does NOT commit — the caller's transaction
scope (get_db dependency or session_scope) owns commit/rollback, which is what
makes "journal + balance in the same transaction" true.
"""

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.billing import (
    BillingJournal,
    BillingProfile,
    JournalKind,
    LedgerAccount,
)

# Fixed Dr/Cr pairs for every kind except `adjustment` (operator-chosen).
KIND_ACCOUNTS: dict[JournalKind, tuple[LedgerAccount, LedgerAccount]] = {
    JournalKind.topup: (LedgerAccount.cash, LedgerAccount.org_wallet),
    JournalKind.usage_charge: (LedgerAccount.org_wallet, LedgerAccount.revenue),
    JournalKind.promo_credit: (LedgerAccount.promo_expense, LedgerAccount.org_wallet),
}


def accounts_for(kind: JournalKind) -> tuple[LedgerAccount, LedgerAccount]:
    """(dr, cr) for a fixed-mapping kind. `adjustment` has no fixed pair."""
    if kind not in KIND_ACCOUNTS:
        raise ValueError(f"{kind} has no fixed account mapping — pass dr/cr explicitly")
    return KIND_ACCOUNTS[kind]


def balance_delta(dr_account: LedgerAccount, cr_account: LedgerAccount, amount_mnt: int) -> int:
    """How an entry moves the org wallet: Cr org_wallet adds, Dr removes."""
    delta = 0
    if cr_account is LedgerAccount.org_wallet:
        delta += amount_mnt
    if dr_account is LedgerAccount.org_wallet:
        delta -= amount_mnt
    return delta


async def get_or_create_profile(db: AsyncSession, org_id: UUID) -> BillingProfile:
    """Lazy-create the org's wallet row. Plain read (no lock)."""
    profile = await db.get(BillingProfile, org_id)
    if profile is None:
        profile = BillingProfile(org_id=org_id, balance_mnt=0)
        db.add(profile)
        await db.flush()
    return profile


async def _profile_for_update(db: AsyncSession, org_id: UUID) -> BillingProfile:
    """Row-locked profile for a balance mutation (serializes concurrent posts)."""
    result = await db.execute(
        select(BillingProfile).where(BillingProfile.org_id == org_id).with_for_update()
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        profile = BillingProfile(org_id=org_id, balance_mnt=0)
        db.add(profile)
        await db.flush()
    return profile


async def post_entry(
    db: AsyncSession,
    *,
    org_id: UUID,
    kind: JournalKind,
    amount_mnt: int,
    description: str,
    dr_account: LedgerAccount | None = None,
    cr_account: LedgerAccount | None = None,
    meta: dict[str, Any] | None = None,
    charge_date: date | None = None,
    created_by_user_id: UUID | None = None,
) -> BillingJournal:
    """Insert one journal row and apply its wallet delta. Flushes, never commits."""
    if amount_mnt <= 0:
        raise ValueError("amount_mnt must be > 0")
    if kind is JournalKind.adjustment:
        if dr_account is None or cr_account is None:
            raise ValueError("adjustment entries need explicit dr_account and cr_account")
    else:
        dr_account, cr_account = accounts_for(kind)

    profile = await _profile_for_update(db, org_id)
    entry = BillingJournal(
        org_id=org_id,
        kind=kind,
        dr_account=dr_account,
        cr_account=cr_account,
        amount_mnt=amount_mnt,
        description=description,
        meta=meta,
        charge_date=charge_date,
        created_by_user_id=created_by_user_id,
    )
    db.add(entry)
    profile.balance_mnt += balance_delta(dr_account, cr_account, amount_mnt)
    await db.flush()
    return entry
