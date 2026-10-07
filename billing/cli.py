"""Local demo: python -m billing.cli [state_file]

Builds a small local billing scenario entirely in memory / a local JSON file
and prints invoice details to the terminal. No network, no external services.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from .engine import BillingEngine, format_invoice
from .models import CustomerConfig, PricePlan, Tier, UsageEvent
from .periods import PeriodRule
from .storage import InMemoryStore, JsonFileStore


def build_engine(store=None) -> BillingEngine:
    plans = {
        "api_calls": PricePlan.tiered(
            "api_calls", "graduated",
            tiers=[
                Tier.create(1000, "0.0100"),
                Tier.create(10000, "0.0050"),
                Tier.create(None, "0.0020"),
            ],
            free_allowance="100",
            cap="500",
        ),
        "storage_gb": PricePlan.flat("storage_gb", "0.50", free_allowance="5"),
        "egress_gb": PricePlan.tiered(
            "egress_gb", "volume",
            tiers=[
                Tier.create(100, "0.10"),
                Tier.create(None, "0.05"),
            ],
        ),
    }
    customers = {
        "cust_a": CustomerConfig("cust_a", currency="CNY", minimum_spend="20.00"),
        "cust_b": CustomerConfig("cust_b", currency="CNY"),
    }
    rule = PeriodRule(timezone="Asia/Shanghai", anchor_day=1)
    return BillingEngine(plans, customers, period_rule=rule, store=store or InMemoryStore())


def main() -> None:
    store = JsonFileStore(sys.argv[1]) if len(sys.argv) > 1 else InMemoryStore()
    engine = build_engine(store)
    tz8 = timezone(timedelta(hours=8))

    events = [
        UsageEvent.create("evt-001", "cust_a", "api_calls", 500, datetime(2026, 1, 5, 10, tzinfo=tz8)),
        UsageEvent.create("evt-002", "cust_a", "api_calls", 800, datetime(2026, 1, 10, 12, tzinfo=tz8)),
        UsageEvent.create("evt-003", "cust_a", "storage_gb", 30, datetime(2026, 1, 12, 8, tzinfo=tz8)),
        UsageEvent.create("evt-004", "cust_a", "egress_gb", 250, datetime(2026, 1, 20, 9, tzinfo=tz8)),
        UsageEvent.create("evt-005", "cust_b", "api_calls", 50, datetime(2026, 1, 3, 9, tzinfo=tz8)),
    ]
    for event in events:
        print(engine.ingest(event))

    # duplicate ingestion of evt-001 -> not billed twice
    print(engine.ingest(events[0]))

    jan = engine.period_rule.period_for(datetime(2026, 1, 15, tzinfo=tz8))
    print(format_invoice(engine.generate_invoice("cust_a", jan.key)))
    print(format_invoice(engine.generate_invoice("cust_b", jan.key)))

    # close January for cust_a, then a late January event arrives
    engine.close_period("cust_a", jan.key)
    late = UsageEvent.create("evt-900", "cust_a", "api_calls", 400,
                             datetime(2026, 1, 28, 9, tzinfo=tz8))
    result = engine.ingest(late)
    print(result)  # late_adjusted -> billed in February

    feb = engine.period_rule.period_for(datetime(2026, 2, 15, tzinfo=tz8))
    print("\n# January invoice after close (unchanged, frozen snapshot):")
    print(format_invoice(engine.generate_invoice("cust_a", jan.key)))
    print("\n# February invoice (contains the late-event adjustment):")
    print(format_invoice(engine.generate_invoice("cust_a", feb.key)))


if __name__ == "__main__":
    main()
