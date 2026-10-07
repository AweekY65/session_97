"""Billing period calculation.

Rules are fixed and fully configurable; the machine's default timezone is
never consulted:

- A period is a calendar month anchored on `anchor_day` (default 1) in the
  configured IANA timezone (default UTC).
- Period boundaries are half-open: [start, end). An event exactly on the
  end boundary belongs to the NEXT period.
- Event timestamps must carry an explicit UTC offset; they are converted
  into the configured timezone before period assignment.
"""
import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class PeriodRule:
    tz_name: str = "UTC"
    anchor_day: int = 1

    def __post_init__(self):
        ZoneInfo(self.tz_name)  # validates the name eagerly
        if not 1 <= self.anchor_day <= 28:
            raise ValueError("anchor_day must be in 1..28 to exist in every month")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


def parse_timestamp(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must carry an explicit UTC offset: {ts!r}")
    return dt


def _anchor(year: int, month: int, rule: PeriodRule) -> datetime:
    day = min(rule.anchor_day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=rule.tz)


def _add_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _sub_month(year: int, month: int) -> Tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def period_for(ts: str, rule: PeriodRule) -> Tuple[datetime, datetime]:
    """Return the half-open [start, end) period containing the timestamp."""
    local = parse_timestamp(ts).astimezone(rule.tz)
    start = _anchor(local.year, local.month, rule)
    if local < start:
        y, m = _sub_month(local.year, local.month)
        start = _anchor(y, m, rule)
    y, m = _add_month(start.year, start.month)
    end = _anchor(y, m, rule)
    return start, end


def next_period(start: datetime, rule: PeriodRule) -> Tuple[datetime, datetime]:
    y, m = _add_month(start.year, start.month)
    end = _anchor(y, m, rule)
    y2, m2 = _add_month(end.year, end.month)
    return end, _anchor(y2, m2, rule)


def period_id(start: datetime, end: datetime) -> str:
    return f"{start.isoformat()}..{end.isoformat()}"
