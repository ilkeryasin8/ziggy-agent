import unittest

from text.plural import count_label, pluralize


class PluralTests(unittest.TestCase):
    def test_regular(self):
        self.assertEqual(pluralize("cat"), "cats")

    def test_label(self):
        self.assertEqual(count_label(3, "dog"), "3 dogs")
