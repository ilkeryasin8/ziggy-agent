"""Money helpers for invoices."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def _dec(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def apply_tax(amount, rate_percent):
    """Return amount plus tax, rounded to cents (half up)."""
    total = _dec(amount) * (1 + _dec(rate_percent) / 100)
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def sum_with_tax(prices, rate_percent):
    """Return the total of prices with tax applied once to the subtotal."""
    subtotal = sum((_dec(p) for p in prices), Decimal(0))
    return apply_tax(subtotal, rate_percent)
