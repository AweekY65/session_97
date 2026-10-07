"""The metering & tiered-billing engine.

Guarantees
----------
- Idempotency: an event id is billed at most once, ever.
- Determinism: the final invoice for a set of events is independent of
  ingestion order (aggregation is commutative; output is canonically sorted).
- Immutability: closing a period snapshots its invoices; late events for a
  closed period never mutate it - they are routed as adjustments into the
  currently open period, or explicitly rejected if no open period exists.

Rule execution order (per metric, per period)
---------------------------------------------
1. aggregate all quantities (normal + adjustment events)
2. subtract the free allowance (quantity) -> billable quantity
3. price the billable quantity (flat / volume / graduated)
4. raise to the minimum spend if below it
5. lower to the cap if above it (cap is applied last and wins)
"""
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Optional

from .models import PricePlan, UsageEvent, price_usage
from .money import money, to_decimal
from .periods import PeriodRule, period_for, period_id

APPLIED = "applied"
DUPLICATE = "duplicate"
ADJUSTED = "adjusted"
REJECTED = "rejected"


class BillingEngine:
    def __init__(self, plans: List[PricePlan], rule: PeriodRule = None, store=None):
        self.plans: Dict[str, PricePlan] = {p.metric: p for p in plans}
        self.rule = rule or PeriodRule()
        self.store = store
        if self.store is None:
            from .store import MemoryStore
            self.store = MemoryStore()

    # ---------------- ingestion ----------------

    def ingest(self, event: UsageEvent, now: Optional[str] = None) -> dict:
        if self.store.has_event(event.event_id):
            return {"status": DUPLICATE, "event_id": event.event_id}
        start, end = period_for(event.timestamp, self.rule)
        pid = period_id(start, end)
        record = {
            "event_id": event.event_id,
            "customer_id": event.customer_id,
            "metric": event.metric,
            "timestamp": event.timestamp,
            "quantity": str(event.quantity),
            "period_id": pid,
            "adjustment": False,
            "original_period_id": None,
        }
        if not self.store.is_closed(pid):
            self.store.append_event(record)
            return {"status": APPLIED, "event_id": event.event_id, "period_id": pid}
        # Late event: the closed period is immutable. Route into the period
        # containing `now` as an adjustment, or reject explicitly.
        if now is None:
            return {"status": REJECTED, "event_id": event.event_id,
                    "reason": f"period {pid} is closed and no 'now' was given"}
        nstart, nend = period_for(now, self.rule)
        npid = period_id(nstart, nend)
        if self.store.is_closed(npid):
            return {"status": REJECTED, "event_id": event.event_id,
                    "reason": f"period {pid} is closed and current period {npid} is also closed"}
        record["period_id"] = npid
        record["adjustment"] = True
        record["original_period_id"] = pid
        self.store.append_event(record)
        return {"status": ADJUSTED, "event_id": event.event_id,
                "period_id": npid, "original_period_id": pid}

    # ---------------- closing ----------------

    def close_period_containing(self, timestamp: str) -> List[dict]:
        """Close the period containing `timestamp`; snapshot all its invoices."""
        start, end = period_for(timestamp, self.rule)
        pid = period_id(start, end)
        if self.store.is_closed(pid):
            raise ValueError(f"period already closed: {pid}")
        customers = sorted({e["customer_id"] for e in self.store.all_events()
                            if e["period_id"] == pid})
        invoices = []
        for cid in customers:
            inv = self._compute_invoice(cid, pid, start, end, status="closed")
            self.store.save_invoice(cid, pid, inv)
            invoices.append(inv)
        self.store.close_period(pid, {"period_id": pid,
                                      "customers": customers})
        return invoices

    # ---------------- invoicing ----------------

    def invoice(self, customer_id: str, timestamp: str) -> dict:
        """Invoice for the period containing `timestamp` (snapshot if closed)."""
        start, end = period_for(timestamp, self.rule)
        pid = period_id(start, end)
        if self.store.is_closed(pid):
            snap = self.store.get_invoice(customer_id, pid)
            if snap is not None:
                return snap
        return self._compute_invoice(customer_id, pid, start, end, status="open")

    def _compute_invoice(self, customer_id, pid, start, end, status) -> dict:
        events = [e for e in self.store.all_events()
                  if e["customer_id"] == customer_id and e["period_id"] == pid]
        # Canonical order makes every downstream artifact deterministic.
        events.sort(key=lambda e: (e["timestamp"], e["event_id"]))
        qty_by_metric: Dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        metrics_seen = set()
        adjustments = []
        for e in events:
            metrics_seen.add(e["metric"])
            qty_by_metric[e["metric"]] += to_decimal(e["quantity"])
            if e["adjustment"]:
                adjustments.append({
                    "event_id": e["event_id"],
                    "metric": e["metric"],
                    "quantity": e["quantity"],
                    "original_period_id": e["original_period_id"],
                })
        lines = []
        total = Decimal(0)
        for metric in sorted(metrics_seen):
            plan = self.plans.get(metric)
            if plan is None:
                lines.append({"metric": metric, "error": "no price plan",
                              "amount": "0"})
                continue
            line = self._price_line(plan, qty_by_metric[metric])
            lines.append(line)
            total += to_decimal(line["amount"])
        return {
            "customer_id": customer_id,
            "period_id": pid,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "timezone": self.rule.tz_name,
            "status": status,
            "currency": next(iter(self.plans.values())).currency if self.plans else "USD",
            "lines": lines,
            "adjustments": sorted(adjustments, key=lambda a: a["event_id"]),
            "total": str(money(total)),
        }

    @staticmethod
    def _price_line(plan: PricePlan, total_qty: Decimal) -> dict:
        # 2. free allowance
        billable = max(Decimal(0), total_qty - plan.free_allowance)
        # 3. tiered pricing
        charges = price_usage(plan, billable)
        subtotal = money(sum((c.amount for c in charges), Decimal(0)))
        amount = subtotal
        minimum_applied = False
        cap_applied = False
        # 4. minimum spend
        if plan.minimum is not None and amount < plan.minimum:
            amount = plan.minimum
            minimum_applied = True
        # 5. cap (applied last; wins over minimum)
        if plan.cap is not None and amount > plan.cap:
            amount = plan.cap
            cap_applied = True
        return {
            "metric": plan.metric,
            "model": plan.model,
            "total_quantity": str(total_qty),
            "free_allowance": str(plan.free_allowance),
            "billable_quantity": str(billable),
            "tier_charges": [
                {"tier": c.tier_index,
                 "up_to": None if c.up_to is None else str(c.up_to),
                 "unit_price": str(c.unit_price),
                 "units": str(c.units),
                 "amount": str(money(c.amount))}
                for c in charges
            ],
            "subtotal": str(subtotal),
            "minimum_applied": minimum_applied,
            "cap_applied": cap_applied,
            "amount": str(money(amount)),
        }
