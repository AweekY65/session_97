"""Core domain models. Everything is plain data; persistence is JSON/local."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from .money import D

FLAT = "flat"
VOLUME = "volume"
GRADUATED = "graduated"


@dataclass(frozen=True)
class UsageEvent:
    event_id: str
    customer_id: str
    metric: str
    quantity: Decimal
    timestamp: datetime  # must be timezone-aware

    @staticmethod
    def create(event_id: str, customer_id: str, metric: str, quantity, timestamp: datetime) -> "UsageEvent":
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        q = D(quantity)
        if q < 0:
            raise ValueError("quantity must be >= 0")
        return UsageEvent(event_id, customer_id, metric, q, timestamp)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "metric": self.metric,
            "quantity": str(self.quantity),
            "timestamp": self.timestamp.isoformat(),
        }

    @staticmethod
    def from_dict(d: dict) -> "UsageEvent":
        return UsageEvent(
            event_id=d["event_id"],
            customer_id=d["customer_id"],
            metric=d["metric"],
            quantity=D(d["quantity"]),
            timestamp=datetime.fromisoformat(d["timestamp"]),
        )


@dataclass(frozen=True)
class Tier:
    """A tier covers (lower_bound, up_to]; up_to=None means infinity.

    Boundary rule: a quantity exactly equal to `up_to` belongs to THIS tier,
    not the next one.
    """
    up_to: Optional[Decimal]
    unit_price: Decimal

    @staticmethod
    def create(up_to, unit_price) -> "Tier":
        return Tier(None if up_to is None else D(up_to), D(unit_price))


@dataclass(frozen=True)
class PricePlan:
    metric: str
    model: str  # FLAT | VOLUME | GRADUATED
    unit_price: Decimal = Decimal("0")       # FLAT only
    tiers: tuple = ()                        # VOLUME / GRADUATED
    free_allowance: Decimal = Decimal("0")   # free units per period
    cap: Optional[Decimal] = None            # max charge for this metric per period

    @staticmethod
    def flat(metric, unit_price, free_allowance="0", cap=None) -> "PricePlan":
        return PricePlan(metric, FLAT, D(unit_price), (), D(free_allowance),
                         None if cap is None else D(cap))

    @staticmethod
    def tiered(metric, model, tiers, free_allowance="0", cap=None) -> "PricePlan":
        if model not in (VOLUME, GRADUATED):
            raise ValueError(model)
        ts = tuple(tiers)
        for prev, cur in zip(ts, ts[1:]):
            if prev.up_to is None:
                raise ValueError("only the last tier may be open-ended")
            if cur.up_to is not None and cur.up_to <= prev.up_to:
                raise ValueError("tier boundaries must be strictly increasing")
        return PricePlan(metric, model, Decimal("0"), ts, D(free_allowance),
                         None if cap is None else D(cap))


@dataclass(frozen=True)
class CustomerConfig:
    customer_id: str
    currency: str = "CNY"
    minimum_spend: Decimal = Decimal("0")  # invoice-level minimum per period


@dataclass(frozen=True)
class TierCharge:
    lower: Decimal
    upper: Optional[Decimal]
    units: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass
class InvoiceLine:
    metric: str
    gross_quantity: Decimal      # before free allowance
    billable_quantity: Decimal   # after free allowance
    amount: Decimal              # after cap
    capped: bool
    breakdown: List[TierCharge] = field(default_factory=list)


@dataclass
class Adjustment:
    event_id: str
    metric: str
    quantity: Decimal
    original_period: str


@dataclass
class Invoice:
    customer_id: str
    period_key: str
    period_start: str
    period_end: str
    currency: str
    lines: List[InvoiceLine]
    adjustments: List[Adjustment]
    subtotal: Decimal
    minimum_spend: Decimal
    total: Decimal
    status: str = "open"  # open | closed
