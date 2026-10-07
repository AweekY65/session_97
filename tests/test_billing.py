"""Automated tests for the local metering & tiered billing engine.

Every test prints the invoice detail it produced to the terminal.
Run:  python -m unittest discover -s tests -v
"""
import random
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from billing import (
    ACCEPTED, CONFLICT, DUPLICATE, LATE_ADJUSTED,
    BillingEngine, CustomerConfig, PricePlan, PeriodRule, Tier, UsageEvent,
    format_invoice,
)
from billing.money import D, money

TZ8 = timezone(timedelta(hours=8))
UTC = timezone.utc


def make_engine(**overrides):
    plans = {
        "api": PricePlan.tiered(
            "api", "graduated",
            tiers=[Tier.create(100, "0.10"), Tier.create(1000, "0.05"), Tier.create(None, "0.01")],
            free_allowance=overrides.get("api_free", "0"),
            cap=overrides.get("api_cap"),
        ),
        "gb": PricePlan.flat("gb", "0.50", free_allowance=overrides.get("gb_free", "0")),
        "net": PricePlan.tiered(
            "net", "volume",
            tiers=[Tier.create(100, "0.10"), Tier.create(None, "0.05")],
        ),
    }
    customers = {
        "c1": CustomerConfig("c1", "CNY", minimum_spend=overrides.get("min_spend", "0")),
    }
    rule = PeriodRule(timezone=overrides.get("tz", "UTC"), anchor_day=overrides.get("anchor", 1))
    return BillingEngine(plans, customers, period_rule=rule)


def ev(eid, metric, qty, ts, customer="c1"):
    return UsageEvent.create(eid, customer, metric, qty, ts)


def jan(key_day=15):
    return datetime(2026, 1, key_day, 12, tzinfo=UTC)


def show(engine, customer="c1", period_key="2026-01-01"):
    inv = engine.generate_invoice(customer, period_key)
    print("\n" + format_invoice(inv))
    return inv


class TestTierBoundaries(unittest.TestCase):
    def test_graduated_boundaries(self):
        engine = make_engine()
        # 100 units: exactly on tier1 boundary -> all in tier1 @0.10 = 10.00
        engine.ingest(ev("e1", "api", 100, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("10.00"))

        engine = make_engine()
        # 101 units: 100 @0.10 + 1 @0.05 = 10.05
        engine.ingest(ev("e1", "api", 101, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("10.05"))

        engine = make_engine()
        # 1000 units: 100 @0.10 + 900 @0.05 = 55.00 (boundary belongs to tier2)
        engine.ingest(ev("e1", "api", 1000, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("55.00"))

        engine = make_engine()
        # 1001 units: 55.00 + 1 @0.01 = 55.01
        engine.ingest(ev("e1", "api", 1001, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("55.01"))

    def test_volume_boundaries(self):
        engine = make_engine()
        # 100 units: boundary -> lower tier price 0.10 for ALL units = 10.00
        engine.ingest(ev("e1", "net", 100, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("10.00"))

        engine = make_engine()
        # 101 units: whole volume jumps to 0.05 -> 5.05
        engine.ingest(ev("e1", "net", 101, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("5.05"))


class TestRuleOrder(unittest.TestCase):
    def test_free_allowance(self):
        engine = make_engine(api_free="100")
        engine.ingest(ev("e1", "api", 250, jan()))
        inv = show(engine)
        # billable = 150 -> 100 @0.10 + 50 @0.05 = 12.50
        self.assertEqual(inv.lines[0].gross_quantity, D("250"))
        self.assertEqual(inv.lines[0].billable_quantity, D("150"))
        self.assertEqual(inv.total, D("12.50"))

    def test_free_allowance_exceeds_usage(self):
        engine = make_engine(api_free="500")
        engine.ingest(ev("e1", "api", 300, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("0.00"))

    def test_cap(self):
        engine = make_engine(api_cap="30.00")
        engine.ingest(ev("e1", "api", 1000, jan()))  # would be 55.00
        inv = show(engine)
        self.assertTrue(inv.lines[0].capped)
        self.assertEqual(inv.total, D("30.00"))

    def test_minimum_spend(self):
        engine = make_engine(min_spend="20.00")
        engine.ingest(ev("e1", "gb", 10, jan()))  # 10 * 0.50 = 5.00 < 20.00
        inv = show(engine)
        self.assertEqual(inv.subtotal, D("5.00"))
        self.assertEqual(inv.total, D("20.00"))

    def test_order_free_then_price_then_cap_then_minimum(self):
        # free allowance first, cap per metric, minimum spend last on the total
        engine = make_engine(api_free="100", api_cap="40.00", min_spend="100.00")
        engine.ingest(ev("e1", "api", 1100, jan()))
        # billable 1000 -> 55.00 -> capped 40.00; total max(40, 100) = 100
        inv = show(engine)
        self.assertEqual(inv.subtotal, D("40.00"))
        self.assertEqual(inv.total, D("100.00"))


class TestIdempotency(unittest.TestCase):
    def test_duplicate_event_not_billed_twice(self):
        engine = make_engine()
        event = ev("e1", "api", 100, jan())
        self.assertEqual(engine.ingest(event).status, ACCEPTED)
        self.assertEqual(engine.ingest(event).status, DUPLICATE)
        self.assertEqual(engine.ingest(event).status, DUPLICATE)
        inv = show(engine)
        self.assertEqual(inv.lines[0].gross_quantity, D("100"))
        self.assertEqual(inv.total, D("10.00"))

    def test_same_id_different_payload_is_conflict(self):
        engine = make_engine()
        engine.ingest(ev("e1", "api", 100, jan()))
        result = engine.ingest(ev("e1", "api", 999, jan()))
        self.assertEqual(result.status, CONFLICT)
        inv = show(engine)
        self.assertEqual(inv.lines[0].gross_quantity, D("100"))


class TestLateEvents(unittest.TestCase):
    def test_late_event_goes_to_next_period_adjustment(self):
        engine = make_engine()
        engine.ingest(ev("e1", "api", 100, jan()))
        before = engine.close_period("c1", "2026-01-01")
        self.assertEqual(before.status, "closed")

        late = ev("e2", "api", 50, jan(28))
        result = engine.ingest(late)
        self.assertEqual(result.status, LATE_ADJUSTED)
        self.assertEqual(result.original_period_key, "2026-01-01")
        self.assertEqual(result.period_key, "2026-02-01")

        after = show(engine)  # closed January invoice must be unchanged
        self.assertEqual(after.total, before.total)
        self.assertEqual(after.status, "closed")

        feb = show(engine, period_key="2026-02-01")
        self.assertEqual(len(feb.adjustments), 1)
        self.assertEqual(feb.adjustments[0].event_id, "e2")
        self.assertEqual(feb.adjustments[0].original_period, "2026-01-01")
        self.assertEqual(feb.lines[0].gross_quantity, D("50"))

    def test_multiple_closed_periods_skip_to_first_open(self):
        engine = make_engine()
        engine.close_period("c1", "2026-01-01")
        engine.close_period("c1", "2026-02-01")
        result = engine.ingest(ev("e9", "api", 10, jan()))
        self.assertEqual(result.status, LATE_ADJUSTED)
        self.assertEqual(result.period_key, "2026-03-01")
        mar = show(engine, period_key="2026-03-01")
        self.assertEqual(mar.lines[0].gross_quantity, D("10"))


class TestOrderIndependence(unittest.TestCase):
    def test_shuffled_input_same_invoice(self):
        events = [
            ev(f"e{i:03d}", "api", str(i * 7 % 130), jan(1 + i % 27)) for i in range(60)
        ] + [
            ev(f"g{i:03d}", "gb", str(i % 11), jan(1 + i % 27)) for i in range(20)
        ]
        invoices = []
        for seed in range(5):
            engine = make_engine()
            shuffled = list(events)
            random.Random(seed).shuffle(shuffled)
            for event in shuffled:
                engine.ingest(event)
            inv = engine.generate_invoice("c1", "2026-01-01")
            invoices.append(inv)
        show(engine)
        first = invoices[0]
        for inv in invoices[1:]:
            self.assertEqual(inv.total, first.total)
            self.assertEqual(
                [(l.metric, l.gross_quantity, l.amount) for l in inv.lines],
                [(l.metric, l.gross_quantity, l.amount) for l in first.lines],
            )


class TestAmounts(unittest.TestCase):
    def test_large_quantity(self):
        engine = make_engine()
        engine.ingest(ev("big", "api", 10**12, jan()))
        inv = show(engine)
        # 100*0.10 + 900*0.05 + (10^12-1000)*0.01
        expected = money(D("10") + D("45") + (D(10**12) - D(1000)) * D("0.01"))
        self.assertEqual(inv.total, expected)

    def test_decimal_prices_and_fractional_quantity(self):
        engine = make_engine()
        engine.plans["api"] = PricePlan.tiered(
            "api", "graduated",
            tiers=[Tier.create("0.5", "0.0001"), Tier.create(None, "0.00005")],
        )
        engine.ingest(ev("e1", "api", "1.5", jan()))
        engine.ingest(ev("e2", "api", "0.25", jan()))
        inv = show(engine)
        # total 1.75 -> 0.5*0.0001 + 1.25*0.00005 = 0.0001125 -> 0.00 (cent rounding)
        self.assertEqual(inv.lines[0].billable_quantity, D("1.75"))
        self.assertEqual(inv.total, D("0.00"))

    def test_decimal_price_exact_accumulation(self):
        engine = make_engine()
        engine.plans["gb"] = PricePlan.flat("gb", "0.333")
        for i in range(3):
            engine.ingest(ev(f"e{i}", "gb", 1, jan()))
        inv = show(engine)
        self.assertEqual(inv.total, D("1.00"))  # 3 * 0.333 = 0.999 -> 1.00


class TestPeriodsAndTimezone(unittest.TestCase):
    def test_fixed_timezone_period_assignment(self):
        # 2026-01-31 23:30 UTC == 2026-02-01 07:30 in Asia/Shanghai
        engine = make_engine(tz="Asia/Shanghai")
        engine.ingest(ev("e1", "api", 10, datetime(2026, 1, 31, 23, 30, tzinfo=UTC)))
        inv = engine.generate_invoice("c1", "2026-02-01")
        print("\n" + format_invoice(inv))
        self.assertEqual(inv.lines[0].gross_quantity, D("10"))
        self.assertEqual(engine.generate_invoice("c1", "2026-01-01").lines, [])

    def test_anchor_day_periods(self):
        engine = make_engine(anchor=15)
        engine.ingest(ev("e1", "api", 10, datetime(2026, 1, 14, 12, tzinfo=UTC)))
        engine.ingest(ev("e2", "api", 20, datetime(2026, 1, 16, 12, tzinfo=UTC)))
        prev = engine.generate_invoice("c1", "2025-12-15")
        cur = engine.generate_invoice("c1", "2026-01-15")
        print("\n" + format_invoice(prev))
        print("\n" + format_invoice(cur))
        self.assertEqual(prev.lines[0].gross_quantity, D("10"))
        self.assertEqual(cur.lines[0].gross_quantity, D("20"))

    def test_anchor_day_clamped_for_short_months(self):
        rule = PeriodRule("UTC", anchor_day=31)
        period = rule.period_for(datetime(2026, 2, 20, tzinfo=UTC))
        # Feb 2026 has 28 days: the period containing Feb 20 is [Jan 31, Feb 28)
        self.assertEqual(period.start.day, 31)
        self.assertEqual(period.end.day, 28)
        nxt = rule.period_for(datetime(2026, 3, 5, tzinfo=UTC))
        self.assertEqual((nxt.start.month, nxt.start.day), (2, 28))
        self.assertEqual((nxt.end.month, nxt.end.day), (3, 31))


if __name__ == "__main__":
    unittest.main(verbosity=2)
