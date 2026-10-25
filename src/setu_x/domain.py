"""Deterministic demo business logic shared by inventory, pricing and gateway.

All values are derived from a SHA-256 hash of the SKU, so every instance (and every test)
computes identical answers with no shared state or database. Money is handled in integer
cents to avoid floating-point rounding. Prices are DEMO values, not real prices.
"""
from __future__ import annotations

import hashlib
from typing import Literal

SKU_PATTERN = r"^SKU-[0-9]{3}$"
CURRENCY = "DEMO"
MAX_STOCK = 50
TIER_DISCOUNT_PCT: dict[str, int] = {"standard": 0, "gold": 10}
Tier = Literal["standard", "gold"]


def _h(sku: str, salt: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{sku}".encode()).digest()[:8], "big")


def stock_for(sku: str) -> int:
    """Units available for a SKU, 0..MAX_STOCK."""
    return _h(sku, "stock") % (MAX_STOCK + 1)


def unit_price_cents(sku: str) -> int:
    """List price per unit in cents, 500..9999 (5.00 to 99.99 DEMO)."""
    return 500 + _h(sku, "price") % 9500


def total_cents(unit_cents: int, quantity: int, discount_pct: int) -> int:
    """Total after percentage discount, rounded half-up to the nearest cent (integer arithmetic)."""
    return (unit_cents * quantity * (100 - discount_pct) + 50) // 100


def quote(sku: str, quantity: int, tier: Tier) -> dict:
    unit = unit_price_cents(sku)
    discount = TIER_DISCOUNT_PCT[tier]
    return {
        "unit_price_cents": unit,
        "discount_pct": discount,
        "total_cents": total_cents(unit, quantity, discount),
        "currency": CURRENCY,
    }


def list_price_quote(sku: str, quantity: int) -> dict:
    """Fallback used by the gateway when pricing is unavailable: list price, no tier discount."""
    unit = unit_price_cents(sku)
    return {
        "unit_price_cents": unit,
        "discount_pct": 0,
        "total_cents": total_cents(unit, quantity, 0),
        "currency": CURRENCY,
    }
