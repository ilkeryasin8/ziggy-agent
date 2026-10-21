import unittest

from search.find import search


class SearchTests(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(search(["Apple", "banana", "Cherry"], "AN"), ["banana"])

    def test_order_preserved(self):
        self.assertEqual(search(["bb", "ab", "ba"], "b"), ["bb", "ab", "ba"])

    def test_no_match(self):
        self.assertEqual(search(["a"], "z"), [])
