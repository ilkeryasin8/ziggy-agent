"""Money helpers for invoices."""


def apply_tax(amount, rate_percent):
    """Return amount plus tax, rounded to cents."""
    return round(float(amount) * (1 + float(rate_percent) / 100), 2)


def sum_with_tax(prices, rate_percent):
    """Return the total of prices with tax applied once to the subtotal."""
    return apply_tax(sum(prices), rate_percent)
