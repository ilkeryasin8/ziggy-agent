import unittest
from decimal import Decimal

from shop.money import apply_tax, sum_with_tax


class HiddenMoneyTests(unittest.TestCase):
    def test_returns_decimal(self):
        self.assertIsInstance(apply_tax(Decimal("1"), 0), Decimal)
        self.assertEqual(str(apply_tax(Decimal("100.00"), Decimal("10"))), "110.00")

    def test_half_up(self):
        self.assertEqual(apply_tax(Decimal("2.675"), 0), Decimal("2.68"))
        self.assertEqual(apply_tax(Decimal("0.05"), 10), Decimal("0.06"))
        self.assertEqual(apply_tax(Decimal("0.125"), 0), Decimal("0.13"))

    def test_float_and_str_inputs(self):
        self.assertEqual(apply_tax(19.99, 0), Decimal("19.99"))
        self.assertEqual(apply_tax("19.99", "8.25"), Decimal("21.64"))

    def test_sum_rounds_once(self):
        self.assertEqual(sum_with_tax([Decimal("1.01"), Decimal("1.01")], 50), Decimal("3.03"))

    def test_sum_exact(self):
        self.assertEqual(sum_with_tax([Decimal("0.10"), Decimal("0.20")], 0), Decimal("0.30"))

    def test_empty_sum(self):
        self.assertEqual(sum_with_tax([], 20), Decimal("0.00"))
