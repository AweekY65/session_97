"""End-to-end tests for the local metering & tiered billing engine.

Run with:  python3 -m pytest tests/ -s
Every test prints the invoice detail it asserts on.
"""
import json
import random
from decimal import Decimal

import pytest

from billing.engine import BillingEngine, APPLIED, ADJUSTED, DUPLICATE, REJECTED
from billing.models import PricePlan, Tier, UsageEvent
from billing.periods import PeriodRule, period_for, period_id
from billing.store import FileStore, MemoryStore


def show(title, invoice):
    print(f"\n===== {title} =====")
    print(json.dumps(invoice, indent=2, sort_keys=True))


def graduated_plan(**kw):
    return PricePlan(metric="api_calls", model="graduated",
                     tiers=(Tier.of(100, "0.10"), Tier.of(500, "0.05"),
                            Tier.of(None, "0.01")), **kw)


def volume_plan(**kw):
    return PricePlan(metric="storage_gb", model="volume",
                     tiers=(Tier.of(100, "0.10"), Tier.of(500, "0.08"),
                            Tier.of(None, "0.05")), **kw)


def flat_plan(metric="seats", price="9.99", **kw):
    return PricePlan(metric=metric, model="flat", unit_price=Decimal(price), **kw)


def make_engine(plans, rule=None, store=None):
    return BillingEngine(plans, rule or PeriodRule("UTC", 1), store or MemoryStore())


def ev(eid, metric, qty, ts="2026-01-10T12:00:00+00:00", cid="cust-1"):
    return UsageEvent.of(eid, cid, metric, ts, qty)


# ---------------------------------------------------------------- tier edges

def test_graduated_tier_boundaries():
    eng = make_engine([graduated_plan()])
    cases = [("99", "9.90"), ("100", "10.00"), ("101", "10.05"),
             ("500", "30.00"), ("501", "30.01"), ("600", "31.00")]
    for i, (qty, expected) in enumerate(cases):
        eng.ingest(ev(f"g{i}", "api_calls", qty))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("graduated boundaries: 99/100/101/500/501/600", inv)
    line = inv["lines"][0]
    assert line["total_quantity"] == "1901"
    # 100*0.10 + 400*0.05 + 1401*0.01 = 10 + 20 + 14.01
    assert line["amount"] == "44.01"
    assert inv["total"] == "44.01"
    # per-case checks on isolated customers
    for i, (qty, expected) in enumerate(cases):
        eng2 = make_engine([graduated_plan()])
        eng2.ingest(ev("x", "api_calls", qty))
        single = eng2.invoice("cust-1", "2026-01-20T00:00:00+00:00")
        assert single["lines"][0]["amount"] == expected, qty


def test_volume_tier_boundaries():
    cases = [("100", "10.00"), ("101", "8.08"), ("500", "40.00"), ("501", "25.05")]
    for qty, expected in cases:
        eng = make_engine([volume_plan()])
        eng.ingest(ev("v", "storage_gb", qty))
        inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
        show(f"volume boundary qty={qty}", inv)
        assert inv["lines"][0]["amount"] == expected, qty
        assert inv["lines"][0]["tier_charges"][0]["units"] == qty


# ------------------------------------------------------- free/min/cap order

def test_free_allowance_subtracted_before_pricing():
    plan = flat_plan(price="0.10", free_allowance=Decimal(100))
    eng = make_engine([plan])
    eng.ingest(ev("f1", "seats", "150"))
    eng.ingest(ev("f2", "seats", "80", cid="cust-2"))
    inv1 = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    inv2 = eng.invoice("cust-2", "2026-01-20T00:00:00+00:00")
    show("free allowance: 150 used, 100 free", inv1)
    show("free allowance: 80 used < 100 free", inv2)
    assert inv1["lines"][0]["billable_quantity"] == "50"
    assert inv1["lines"][0]["amount"] == "5.00"
    assert inv2["lines"][0]["billable_quantity"] == "0"
    assert inv2["lines"][0]["amount"] == "0.00"


def test_minimum_then_cap_order():
    # below minimum -> raised to minimum
    eng = make_engine([flat_plan(price="0.10", minimum=Decimal("5.00"))])
    eng.ingest(ev("m1", "seats", "10"))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("minimum spend: subtotal 1.00 -> 5.00", inv)
    assert inv["lines"][0]["subtotal"] == "1.00"
    assert inv["lines"][0]["minimum_applied"] is True
    assert inv["lines"][0]["amount"] == "5.00"
    # above cap -> lowered to cap
    eng = make_engine([flat_plan(price="0.10", cap=Decimal("50.00"))])
    eng.ingest(ev("c1", "seats", "1000"))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("cap: subtotal 100.00 -> 50.00", inv)
    assert inv["lines"][0]["cap_applied"] is True
    assert inv["lines"][0]["amount"] == "50.00"
    # cap applied last: minimum 60 + cap 50 -> final 50
    eng = make_engine([flat_plan(price="0.10", minimum=Decimal("60.00"),
                                 cap=Decimal("50.00"))])
    eng.ingest(ev("mc1", "seats", "10"))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("minimum 60 vs cap 50: cap wins -> 50.00", inv)
    assert inv["lines"][0]["minimum_applied"] is True
    assert inv["lines"][0]["cap_applied"] is True
    assert inv["lines"][0]["amount"] == "50.00"


# ------------------------------------------------------------- idempotency

def test_duplicate_event_id_billed_once():
    eng = make_engine([flat_plan(price="0.10")])
    r1 = eng.ingest(ev("dup-1", "seats", "10"))
    r2 = eng.ingest(ev("dup-1", "seats", "10"))
    r3 = eng.ingest(ev("dup-1", "seats", "999"))  # same id, different payload
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("duplicate event id -> billed once", inv)
    assert r1["status"] == APPLIED
    assert r2["status"] == DUPLICATE and r3["status"] == DUPLICATE
    assert inv["lines"][0]["total_quantity"] == "10"
    assert inv["total"] == "1.00"


# -------------------------------------------------------------- late events

def test_late_event_becomes_adjustment_next_period():
    eng = make_engine([flat_plan(price="0.10")])
    eng.ingest(ev("jan-1", "seats", "10", ts="2026-01-10T00:00:00+00:00"))
    eng.close_period_containing("2026-01-15T00:00:00+00:00")
    closed_before = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    # late January event arrives while February is open
    r = eng.ingest(ev("jan-late", "seats", "5", ts="2026-01-25T00:00:00+00:00"),
                   now="2026-02-03T00:00:00+00:00")
    closed_after = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    feb = eng.invoice("cust-1", "2026-02-10T00:00:00+00:00")
    show("closed January invoice (unchanged)", closed_after)
    show("February invoice carrying the adjustment", feb)
    assert r["status"] == ADJUSTED
    assert closed_before == closed_after            # closed invoice immutable
    assert closed_after["total"] == "1.00"
    assert feb["lines"][0]["total_quantity"] == "5"
    assert feb["total"] == "0.50"
    assert feb["adjustments"] == [{
        "event_id": "jan-late", "metric": "seats", "quantity": "5",
        "original_period_id": closed_after["period_id"]}]


def test_late_event_rejected_without_open_period():
    eng = make_engine([flat_plan()])
    eng.ingest(ev("jan-1", "seats", "10", ts="2026-01-10T00:00:00+00:00"))
    eng.close_period_containing("2026-01-15T00:00:00+00:00")
    r = eng.ingest(ev("jan-late", "seats", "5", ts="2026-01-25T00:00:00+00:00"))
    assert r["status"] == REJECTED
    assert "closed" in r["reason"]
    # rejected events are not billed anywhere
    feb = eng.invoice("cust-1", "2026-02-10T00:00:00+00:00")
    show("rejected late event bills nothing", feb)
    assert feb["total"] == "0.00"


# ------------------------------------------------------------ determinism

def test_invoice_independent_of_ingestion_order():
    plans = [graduated_plan(free_allowance=Decimal(10)),
             volume_plan(), flat_plan(price="0.25", cap=Decimal("500.00"))]
    events = []
    for i in range(40):
        events.append(UsageEvent.of(f"e{i:02d}", f"cust-{i % 3}",
                                    ["api_calls", "storage_gb", "seats"][i % 3],
                                    f"2026-01-{(i % 28) + 1:02d}T{(i * 7) % 24:02d}:00:00+00:00",
                                    str(Decimal(i) * Decimal("3.5") + 1)))
    invoices_per_order = []
    for seed in (1, 2, 3):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        eng = make_engine(plans)
        for e in shuffled:
            assert eng.ingest(e)["status"] == APPLIED
        invs = {cid: eng.invoice(cid, "2026-01-31T00:00:00+00:00")
                for cid in ("cust-0", "cust-1", "cust-2")}
        invoices_per_order.append(invs)
    show("order-independence: cust-0 invoice (identical for 3 shuffles)",
         invoices_per_order[0]["cust-0"])
    assert invoices_per_order[0] == invoices_per_order[1] == invoices_per_order[2]


# ------------------------------------------------- big numbers / decimals

def test_large_quantity_and_fractional_price():
    plan = flat_plan(metric="tokens", price="0.0001")
    eng = make_engine([plan])
    eng.ingest(ev("big", "tokens", "1000000000"))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("1e9 units @ 0.0001", inv)
    assert inv["lines"][0]["amount"] == "100000.00"
    assert inv["total"] == "100000.00"


def test_fractional_quantity_and_decimal_price():
    plan = flat_plan(metric="gb", price="0.007")
    eng = make_engine([plan])
    eng.ingest(ev("frac-1", "gb", "0.5"))
    eng.ingest(ev("frac-2", "gb", "1.25"))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("1.75 GB @ 0.007 = 0.01225 -> 0.01 (ROUND_HALF_UP)", inv)
    assert inv["lines"][0]["total_quantity"] == "1.75"
    assert inv["lines"][0]["amount"] == "0.01"
    # graduated with fractional quantity crossing a boundary
    eng = make_engine([graduated_plan()])
    eng.ingest(ev("frac-3", "api_calls", "100.5"))
    inv = eng.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("graduated 100.5: 100 @ 0.10 + 0.5 @ 0.05", inv)
    assert inv["lines"][0]["amount"] == "10.03"  # 10 + 0.025 -> half-up


# ----------------------------------------------------- periods & timezone

def test_period_boundaries_and_fixed_timezone():
    rule = PeriodRule("Asia/Shanghai", 1)
    # 2026-01-31 16:00 UTC == 2026-02-01 00:00 +08:00 -> February period
    jan = period_id(*period_for("2026-01-31T15:59:59+00:00", rule))
    feb = period_id(*period_for("2026-01-31T16:00:00+00:00", rule))
    assert jan != feb
    assert "2026-01-01T00:00:00+08:00" in jan
    assert "2026-02-01T00:00:00+08:00" in feb
    # anchor day 15: Jan 20 belongs to the Jan15..Feb15 period
    r15 = PeriodRule("UTC", 15)
    s, e = period_for("2026-01-20T00:00:00+00:00", r15)
    assert (s.day, e.day) == (15, 15) and s.month == 1 and e.month == 2
    s, e = period_for("2026-01-15T00:00:00+00:00", r15)
    assert s.month == 1 and s.day == 15  # boundary instant starts new period
    # naive timestamps are rejected: no machine-default timezone is assumed
    with pytest.raises(ValueError):
        period_for("2026-01-20T00:00:00", rule)
    # events on either side of a boundary land on different invoices
    eng = make_engine([flat_plan(price="1.00")], rule=rule)
    eng.ingest(ev("tz-1", "seats", "1", ts="2026-01-31T15:59:59+00:00"))
    eng.ingest(ev("tz-2", "seats", "1", ts="2026-01-31T16:00:00+00:00"))
    jan_inv = eng.invoice("cust-1", "2026-01-15T00:00:00+08:00")
    feb_inv = eng.invoice("cust-1", "2026-02-15T00:00:00+08:00")
    show("Shanghai tz: event 1 second before boundary -> January", jan_inv)
    show("Shanghai tz: event at boundary -> February", feb_inv)
    assert jan_inv["total"] == "1.00" and feb_inv["total"] == "1.00"


# --------------------------------------------------------- local file store

def test_file_store_persists_and_reloads(tmp_path):
    plans = [flat_plan(price="0.10")]
    eng = make_engine(plans, store=FileStore(str(tmp_path)))
    eng.ingest(ev("p-1", "seats", "10"))
    eng.close_period_containing("2026-01-15T00:00:00+00:00")
    # a brand-new engine over the same directory sees everything
    eng2 = make_engine(plans, store=FileStore(str(tmp_path)))
    assert eng2.ingest(ev("p-1", "seats", "10"))["status"] == DUPLICATE
    inv = eng2.invoice("cust-1", "2026-01-20T00:00:00+00:00")
    show("reloaded from local JSON files (closed period snapshot)", inv)
    assert inv["status"] == "closed"
    assert inv["total"] == "1.00"
