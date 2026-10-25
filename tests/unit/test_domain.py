import pytest

from setu_x import domain


@pytest.mark.parametrize("sku", [f"SKU-{i:03d}" for i in range(0, 1000, 37)])
def test_stock_and_price_ranges(sku):
    assert 0 <= domain.stock_for(sku) <= domain.MAX_STOCK
    assert 500 <= domain.unit_price_cents(sku) <= 9999


def test_deterministic_across_calls():
    assert domain.stock_for("SKU-001") == domain.stock_for("SKU-001")
    assert domain.quote("SKU-001", 3, "gold") == domain.quote("SKU-001", 3, "gold")


def test_golden_values_sku_001():
    # Golden values: if these change, every recorded experiment's expected output changes too.
    assert domain.stock_for("SKU-001") == GOLDEN["stock"]
    assert domain.unit_price_cents("SKU-001") == GOLDEN["unit"]


def test_total_rounding_half_up():
    assert domain.total_cents(999, 1, 10) == 899  # 899.1 -> 899
    assert domain.total_cents(995, 1, 10) == 896  # 895.5 -> 896 (half up)
    assert domain.total_cents(1000, 3, 0) == 3000


def test_gold_discount_applied_and_list_price_has_none():
    q = domain.quote("SKU-002", 4, "gold")
    lp = domain.list_price_quote("SKU-002", 4)
    assert q["discount_pct"] == 10 and lp["discount_pct"] == 0
    assert q["total_cents"] < lp["total_cents"]
    assert q["currency"] == lp["currency"] == "DEMO"


# Computed once from domain.py (SHA-256 based) on 2026-10-25 and pinned here.
GOLDEN = {"stock": 8, "unit": 5500}
