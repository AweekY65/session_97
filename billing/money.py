"""Money handling based on decimal.Decimal (BigDecimal equivalent).

All monetary values are Decimal, never float. Quantities are also Decimal
so that fractional usage (e.g. 0.5 GB) and fractional unit prices
(e.g. 0.0001 / unit) are exact.
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def D(value) -> Decimal:
    """Coerce int/str/Decimal to Decimal without float rounding."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is forbidden for money/quantity; use str or Decimal")
    return Decimal(value)


def money(value) -> Decimal:
    """Quantize a Decimal to minor currency units (cents), ROUND_HALF_UP."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)
