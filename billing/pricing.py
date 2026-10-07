"""Pricing calculators for the three supported models.

Boundary convention (all models): tier i covers (lower_i, up_to_i],
i.e. a quantity exactly equal to a tier boundary is billed in the LOWER tier.
"""
from decimal import Decimal
from typing import List, Optional, Tuple

from .models import GRADUATED, Tier, TierCharge
from .money import D, money


def price_flat(quantity: Decimal, unit_price: Decimal) -> Tuple[Decimal, List[TierCharge]]:
    amount = money(quantity * unit_price)
    return amount, [TierCharge(Decimal("0"), None, quantity, unit_price, amount)]


def _volume_tier(quantity: Decimal, tiers) -> Tier:
    for tier in tiers:
        if tier.up_to is None or quantity <= tier.up_to:
            return tier
    raise ValueError("tier list does not cover quantity")  # unreachable if last tier open


def price_volume(quantity: Decimal, tiers) -> Tuple[Decimal, List[TierCharge]]:
    """Volume pricing: the SINGLE tier matching the total quantity applies to ALL units."""
    if quantity == 0:
        return money(0), []
    tier = _volume_tier(quantity, tiers)
    amount = money(quantity * tier.unit_price)
    return amount, [TierCharge(Decimal("0"), tier.up_to, quantity, tier.unit_price, amount)]


def price_graduated(quantity: Decimal, tiers) -> Tuple[Decimal, List[TierCharge]]:
    """Graduated pricing: each bracket charges only the units inside it."""
    remaining = quantity
    lower = Decimal("0")
    breakdown: List[TierCharge] = []
    total = Decimal("0")
    for tier in tiers:
        if remaining <= 0:
            break
        if tier.up_to is None:
            units = remaining
        else:
            width = tier.up_to - lower
            units = min(remaining, width)
        if units > 0:
            amount = money(units * tier.unit_price)
            total += amount
            breakdown.append(TierCharge(lower, tier.up_to, units, tier.unit_price, amount))
            remaining -= units
        if tier.up_to is not None:
            lower = tier.up_to
    if remaining > 0:
        raise ValueError("tier list does not cover quantity")
    return money(total), breakdown


def price(model: str, quantity: Decimal, unit_price: Decimal, tiers) -> Tuple[Decimal, List[TierCharge]]:
    quantity = D(quantity)
    if model == "flat":
        return price_flat(quantity, D(unit_price))
    if model == "volume":
        return price_volume(quantity, tiers)
    if model == "graduated":
        return price_graduated(quantity, tiers)
    raise ValueError(f"unknown pricing model: {model}")
