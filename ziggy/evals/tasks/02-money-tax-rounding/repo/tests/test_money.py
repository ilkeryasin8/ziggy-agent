import unittest
from decimal import Decimal

from shop.money import apply_tax, sum_with_tax


class MoneyTests(unittest.TestCase):
    def test_simple_tax(self):
        self.assertEqual(apply_tax(Decimal("100"), Decimal("10")), Decimal("110.00"))

    def test_sum(self):
        total = sum_with_tax([Decimal("10"), Decimal("20")], Decimal("10"))
        self.assertEqual(total, Decimal("33.00"))
