"""Billing engine: idempotent ingestion, period aggregation, invoicing.

Rule execution order per metric per period (fixed, documented in README):
  1. aggregate gross quantity (sum of event quantities assigned to the period)
  2. subtract free allowance        -> billable quantity
  3. price via flat / volume / graduated model
  4. apply metric cap               -> line amount
  5. sum lines                      -> invoice subtotal
  6. apply customer minimum spend   -> invoice total
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from .models import (
    Adjustment,
    CustomerConfig,
    Invoice,
    InvoiceLine,
    PricePlan,
    UsageEvent,
)
from .money import money
from .periods import Period, PeriodRule
from .pricing import price
from .storage import InMemoryStore

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
CONFLICT = "conflict"
LATE_ADJUSTED = "late_adjusted"


@dataclass(frozen=True)
class IngestResult:
    status: str
    event_id: str
    period_key: Optional[str] = None        # period the event is billed in
    original_period_key: Optional[str] = None  # set for late events


class BillingEngine:
    def __init__(
        self,
        plans: Dict[str, PricePlan],
        customers: Dict[str, CustomerConfig],
        period_rule: Optional[PeriodRule] = None,
        store: Optional[InMemoryStore] = None,
    ):
        self.plans = plans
        self.customers = customers
        self.period_rule = period_rule or PeriodRule("UTC", anchor_day=1)
        self.store = store or InMemoryStore()
        self._periods: Dict[str, Period] = {}

    # ---- ingestion -----------------------------------------------------

    def ingest(self, event: UsageEvent) -> IngestResult:
        existing = self.store.events.get(event.event_id)
        if existing is not None:
            if existing == event:
                return IngestResult(DUPLICATE, event.event_id,
                                    self.store.assignments.get(event.event_id))
            return IngestResult(CONFLICT, event.event_id,
                                self.store.assignments.get(event.event_id))

        original = self._register(self.period_rule.period_for(event.timestamp))
        target = original
        while (event.customer_id, target.key) in self.store.closed:
            target = self._register(self.period_rule.next_period(target))

        self.store.events[event.event_id] = event
        self.store.assignments[event.event_id] = target.key
        self.store.flush()

        if target.key != original.key:
            return IngestResult(LATE_ADJUSTED, event.event_id, target.key, original.key)
        return IngestResult(ACCEPTED, event.event_id, target.key)

    # ---- lifecycle -----------------------------------------------------

    def close_period(self, customer_id: str, period_key: str) -> Invoice:
        """Freeze the invoice for a period. Late events afterwards are
        reassigned to the next open period as adjustments."""
        key = (customer_id, period_key)
        if key not in self.store.closed:
            invoice = self._build_invoice(customer_id, period_key)
            invoice.status = "closed"
            self.store.closed.add(key)
            self.store.snapshots[key] = self._invoice_to_dict(invoice)
            self.store.flush()
        return self._invoice_from_dict(self.store.snapshots[key])

    def generate_invoice(self, customer_id: str, period_key: str) -> Invoice:
        """Closed periods return the frozen snapshot; open periods are
        recomputed deterministically from the stored events."""
        snap = self.store.snapshots.get((customer_id, period_key))
        if snap is not None:
            return self._invoice_from_dict(snap)
        return self._build_invoice(customer_id, period_key)

    # ---- internals -----------------------------------------------------

    def _register(self, period: Period) -> Period:
        self._periods.setdefault(period.key, period)
        return self._periods[period.key]

    def _build_invoice(self, customer_id: str, period_key: str) -> Invoice:
        customer = self.customers[customer_id]
        # deterministic order: sort by event_id
        assigned = sorted(
            (e for e in self.store.events.values()
             if e.customer_id == customer_id
             and self.store.assignments.get(e.event_id) == period_key),
            key=lambda e: e.event_id,
        )
        quantities: Dict[str, Decimal] = {}
        adjustments: List[Adjustment] = []
        for event in assigned:
            quantities[event.metric] = quantities.get(event.metric, Decimal("0")) + event.quantity
            original = self._register(self.period_rule.period_for(event.timestamp))
            if original.key != period_key:
                adjustments.append(Adjustment(event.event_id, event.metric,
                                              event.quantity, original.key))

        lines: List[InvoiceLine] = []
        subtotal = Decimal("0")
        for metric in sorted(quantities):
            plan = self.plans.get(metric)
            if plan is None:
                raise ValueError(f"no price plan for metric {metric!r}")
            gross = quantities[metric]
            billable = max(Decimal("0"), gross - plan.free_allowance)
            amount, breakdown = price(plan.model, billable, plan.unit_price, plan.tiers)
            capped = False
            if plan.cap is not None and amount > plan.cap:
                amount, capped = plan.cap, True
            lines.append(InvoiceLine(metric, gross, billable, money(amount), capped, breakdown))
            subtotal += amount

        subtotal = money(subtotal)
        total = max(subtotal, money(customer.minimum_spend))
        period = self._periods.get(period_key)
        return Invoice(
            customer_id=customer_id,
            period_key=period_key,
            period_start=period.start.isoformat() if period else "",
            period_end=period.end.isoformat() if period else "",
            currency=customer.currency,
            lines=lines,
            adjustments=adjustments,
            subtotal=subtotal,
            minimum_spend=money(customer.minimum_spend),
            total=total,
        )

    # ---- (de)serialization for snapshots --------------------------------

    @staticmethod
    def _invoice_to_dict(inv: Invoice) -> dict:
        return {
            "customer_id": inv.customer_id,
            "period_key": inv.period_key,
            "period_start": inv.period_start,
            "period_end": inv.period_end,
            "currency": inv.currency,
            "status": inv.status,
            "subtotal": str(inv.subtotal),
            "minimum_spend": str(inv.minimum_spend),
            "total": str(inv.total),
            "lines": [
                {
                    "metric": l.metric,
                    "gross_quantity": str(l.gross_quantity),
                    "billable_quantity": str(l.billable_quantity),
                    "amount": str(l.amount),
                    "capped": l.capped,
                }
                for l in inv.lines
            ],
            "adjustments": [
                {
                    "event_id": a.event_id,
                    "metric": a.metric,
                    "quantity": str(a.quantity),
                    "original_period": a.original_period,
                }
                for a in inv.adjustments
            ],
        }

    @staticmethod
    def _invoice_from_dict(d: dict) -> Invoice:
        from .money import D

        return Invoice(
            customer_id=d["customer_id"],
            period_key=d["period_key"],
            period_start=d["period_start"],
            period_end=d["period_end"],
            currency=d["currency"],
            status=d["status"],
            subtotal=D(d["subtotal"]),
            minimum_spend=D(d["minimum_spend"]),
            total=D(d["total"]),
            lines=[
                InvoiceLine(l["metric"], D(l["gross_quantity"]),
                            D(l["billable_quantity"]), D(l["amount"]), l["capped"])
                for l in d["lines"]
            ],
            adjustments=[
                Adjustment(a["event_id"], a["metric"], D(a["quantity"]), a["original_period"])
                for a in d["adjustments"]
            ],
        )


def format_invoice(inv: Invoice) -> str:
    """Human-readable invoice detail for terminal output."""
    out = [
        "=" * 64,
        f"INVOICE  customer={inv.customer_id}  period={inv.period_key}  "
        f"status={inv.status}  currency={inv.currency}",
        f"window   [{inv.period_start} .. {inv.period_end})",
        "-" * 64,
    ]
    for line in inv.lines:
        out.append(
            f"  {line.metric:<12} gross={line.gross_quantity}  "
            f"billable={line.billable_quantity}  amount={line.amount}"
            + ("  [CAPPED]" if line.capped else "")
        )
        for b in line.breakdown:
            upper = "inf" if b.upper is None else str(b.upper)
            out.append(
                f"      tier ({b.lower}, {upper}]  units={b.units}  "
                f"price={b.unit_price}  amount={b.amount}"
            )
    for adj in inv.adjustments:
        out.append(
            f"  ADJUSTMENT event={adj.event_id} metric={adj.metric} "
            f"qty={adj.quantity} (late data from period {adj.original_period})"
        )
    out.append("-" * 64)
    out.append(f"  subtotal={inv.subtotal}  minimum_spend={inv.minimum_spend}  "
               f"TOTAL={inv.total} {inv.currency}")
    out.append("=" * 64)
    return "\n".join(out)
