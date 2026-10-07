"""Billing periods with a FIXED, configurable timezone and anchor day.

The machine's local timezone is never consulted: every timestamp is converted
into the configured ZoneInfo timezone before period assignment.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Period:
    """Half-open interval [start, end) in the configured timezone."""
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return self.start.date().isoformat()

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def _prev_month(y: int, m: int) -> Tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _next_month(y: int, m: int) -> Tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


class PeriodRule:
    """Monthly periods starting on `anchor_day` (clamped to month length)
    in a fixed IANA timezone, e.g. PeriodRule("Asia/Shanghai", anchor_day=1)."""

    def __init__(self, timezone: str = "UTC", anchor_day: int = 1):
        if not 1 <= anchor_day <= 31:
            raise ValueError("anchor_day must be in 1..31")
        self.tz = ZoneInfo(timezone)
        self.timezone = timezone
        self.anchor_day = anchor_day

    def _month_start(self, y: int, m: int) -> datetime:
        last_day = calendar.monthrange(y, m)[1]
        return datetime(y, m, min(self.anchor_day, last_day), tzinfo=self.tz)

    def period_for(self, ts: datetime) -> Period:
        if ts.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        local = ts.astimezone(self.tz)
        start = self._month_start(local.year, local.month)
        if local < start:
            start = self._month_start(*_prev_month(local.year, local.month))
        end = self._month_start(*_next_month(start.year, start.month))
        return Period(start, end)

    def next_period(self, period: Period) -> Period:
        start = self._month_start(*_next_month(period.start.year, period.start.month))
        end = self._month_start(*_next_month(start.year, start.month))
        return Period(start, end)
