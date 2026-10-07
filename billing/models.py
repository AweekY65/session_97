"""Core domain models: usage events, tiers, price plans, pricing functions.

Pricing models
--------------
flat       : amount = billable_qty * unit_price
volume     : the TOTAL billable quantity falls into exactly one tier;
             amount = billable_qty * that tier's unit_price
graduated  : each unit is priced at the tier bracket it falls in;
             amount = sum over brackets of (units_in_bracket * bracket price)

Tier boundaries
---------------
Tiers are defined by an inclusive upper bound `up_to` (None = unbounded).
Bracket i covers the half-open interval (up_to[i-1], up_to[i]] - the lower
bound is exclusive, the upper bound inclusive. Example with up_to 100 then
500: quantity exactly 100 is entirely in tier 1; quantity 100.000...1 starts
spilling into tier 2.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from .money import to_decimal

FLAT = "flat"
VOLUME = "volume"
GRADUATED = "graduated"
MODELS = (FLAT, VOLUME, GRADUATED)


@dataclass(frozen=True)
class UsageEvent:
    event_id: str
    customer_id: str
    metric: str
    timestamp: str  # ISO 8601, must carry an explicit UTC offset
    quantity: Decimal

    @staticmethod
    def of(event_id, customer_id, metric, timestamp, quantity) -> "UsageEvent":
        return UsageEvent(event_id, customer_id, metric, timestamp, to_decimal(quantity))


@dataclass(frozen=True)
class Tier:
    up_to: Optional[Decimal]  # inclusive upper bound; None = infinity
    unit_price: Decimal

    @staticmethod
    def of(up_to, unit_price) -> "Tier":
        return Tier(None if up_to is None else to_decimal(up_to), to_decimal(unit_price))


@dataclass(frozen=True)
class PricePlan:
    metric: str
    model: str
    currency: str = "USD"
    unit_price: Optional[Decimal] = None   # flat model only
    tiers: tuple = ()                       # volume / graduated models
    free_allowance: Decimal = Decimal(0)    # free quantity per period, subtracted first
    minimum: Optional[Decimal] = None       # minimum charge per period (money)
    cap: Optional[Decimal] = None           # maximum charge per period (money)

    def __post_init__(self):
        if self.model not in MODELS:
            raise ValueError(f"unknown pricing model: {self.model}")
        if self.model == FLAT and self.unit_price is None:
            raise ValueError("flat plan requires unit_price")
        if self.model in (VOLUME, GRADUATED):
            if not self.tiers:
                raise ValueError(f"{self.model} plan requires tiers")
            for prev, cur in zip(self.tiers, self.tiers[1:]):
                if prev.up_to is None:
                    raise ValueError("only the last tier may be unbounded")
                if cur.up_to is not None and cur.up_to <= prev.up_to:
                    raise ValueError("tier up_to bounds must be strictly increasing")


@dataclass(frozen=True)
class TierCharge:
    """One bracket of a graduated/volume computation, for invoice detail."""
    tier_index: int
    up_to: Optional[Decimal]
    unit_price: Decimal
    units: Decimal
    amount: Decimal


def price_usage(plan: PricePlan, billable_qty: Decimal) -> List[TierCharge]:
    """Price a billable quantity (free allowance already subtracted).

    Returns the per-bracket breakdown; the line amount is the sum.
    """
    if billable_qty <= 0:
        return []
    if plan.model == FLAT:
        return [TierCharge(0, None, plan.unit_price, billable_qty,
                           billable_qty * plan.unit_price)]
    if plan.model == VOLUME:
        for i, tier in enumerate(plan.tiers):
            if tier.up_to is None or billable_qty <= tier.up_to:
                return [TierCharge(i, tier.up_to, tier.unit_price, billable_qty,
                                   billable_qty * tier.unit_price)]
        raise AssertionError("unreachable: last tier must be unbounded")
    # graduated: fill each bracket up to its inclusive upper bound
    charges: List[TierCharge] = []
    lower = Decimal(0)
    remaining = billable_qty
    for i, tier in enumerate(plan.tiers):
        if remaining <= 0:
            break
        bracket_size = remaining if tier.up_to is None else min(remaining, tier.up_to - lower)
        charges.append(TierCharge(i, tier.up_to, tier.unit_price, bracket_size,
                                  bracket_size * tier.unit_price))
        remaining -= bracket_size
        if tier.up_to is not None:
            lower = tier.up_to
    return charges
