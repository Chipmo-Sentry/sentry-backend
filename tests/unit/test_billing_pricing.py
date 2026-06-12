"""T14 pricing — tier boundaries and the founder-approved numbers."""

import pytest

from sentry_backend.services.billing.pricing import (
    ENTERPRISE,
    PRO,
    STARTER,
    daily_from_monthly,
    org_daily_charge_mnt,
    org_monthly_charge_mnt,
    store_monthly_mnt,
    tier_for,
)

# --- founder-approved constants (a price change must be a conscious diff) ---


def test_starter_prices() -> None:
    assert STARTER.platform_fee_mnt == 49_000
    assert STARTER.camera_fee_mnt == 22_000


def test_pro_prices() -> None:
    assert PRO.platform_fee_mnt == 39_000
    assert PRO.camera_fee_mnt == 17_000


def test_enterprise_prices() -> None:
    assert ENTERPRISE.platform_fee_mnt == 29_000
    assert ENTERPRISE.camera_fee_mnt == 12_000


# --- tier boundaries ---------------------------------------------------------


@pytest.mark.parametrize(
    ("cams", "tier_name"),
    [
        (1, "starter"),
        (9, "starter"),
        (10, "pro"),
        (50, "pro"),
        (51, "enterprise"),
        (500, "enterprise"),
    ],
)
def test_tier_boundaries(cams: int, tier_name: str) -> None:
    assert tier_for(cams).name == tier_name


def test_tier_for_rejects_zero() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        tier_for(0)


# --- store monthly -----------------------------------------------------------


def test_store_with_no_cameras_is_free() -> None:
    # Платформ төлбөр зөвхөн камертай дэлгүүрт.
    assert store_monthly_mnt(0) == 0


def test_starter_store_monthly() -> None:
    # 49,000 + 3 × 22,000
    assert store_monthly_mnt(3) == 115_000


def test_pro_store_monthly() -> None:
    # 39,000 + 10 × 17,000
    assert store_monthly_mnt(10) == 209_000


def test_enterprise_store_monthly() -> None:
    # 29,000 + 60 × 12,000
    assert store_monthly_mnt(60) == 749_000


def test_tier_applies_to_whole_store_not_marginal() -> None:
    # 9 cams (starter) vs 10 cams (pro): the whole store re-tiers.
    assert store_monthly_mnt(9) == 49_000 + 9 * 22_000  # 247,000
    assert store_monthly_mnt(10) == 209_000  # cheaper platform+per-cam at pro


# --- daily conversion --------------------------------------------------------


def test_daily_is_monthly_div_30_rounded_half_up() -> None:
    assert daily_from_monthly(71_000) == 2_367  # 2366.67 → up
    assert daily_from_monthly(30_000) == 1_000
    assert daily_from_monthly(30_014) == 1_000  # 1000.47 → down
    assert daily_from_monthly(30_015) == 1_001  # exactly .5 → up


def test_daily_of_zero_is_zero() -> None:
    assert daily_from_monthly(0) == 0


def test_org_daily_sums_per_store_rounding() -> None:
    # Each store rounds independently, THEN sums (spec: ÷30 → бүхэлчилж → нийлбэр).
    counts = [1, 1]  # two starter stores, 71,000₮/month each
    assert org_daily_charge_mnt(counts) == 2 * 2_367


def test_org_daily_skips_empty_stores() -> None:
    assert org_daily_charge_mnt([0, 0]) == 0
    assert org_daily_charge_mnt([]) == 0


def test_org_monthly_total() -> None:
    assert org_monthly_charge_mnt([3, 0, 10]) == 115_000 + 0 + 209_000
