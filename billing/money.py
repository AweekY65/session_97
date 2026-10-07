"""Money and quantity arithmetic.

All monetary amounts use decimal.Decimal (BigDecimal-equivalent semantics).
Quantities are also Decimal so fractional usage (e.g. 1.5 GB) is exact.
Money is quantized to a fixed currency scale with ROUND_HALF_UP, exactly
once, at invoice finalization and at each line-item boundary - never via
binary floats.
"""
from decimal import Decimal, ROUND_HALF_UP

CURRENCY_SCALE = 2  # minor units exponent, e.g. 2 -> cents
CENT = Decimal(1).scaleb(-CURRENCY_SCALE)


def to_decimal(value) -> Decimal:
    """Exact conversion. Rejects float to avoid binary rounding surprises."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is not allowed for money/quantity; use str or Decimal")
    return Decimal(value)


def money(value) -> Decimal:
    """Quantize to the currency scale using ROUND_HALF_UP."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
