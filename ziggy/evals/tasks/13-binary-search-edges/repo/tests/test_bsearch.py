import unittest

from algos.bsearch import index_of


class BsearchTests(unittest.TestCase):
    def test_middle(self):
        self.assertEqual(index_of([1, 3, 5, 7, 9, 11], 5), 2)

    def test_missing(self):
        self.assertEqual(index_of([1, 3, 5, 7, 9, 11], 4), -1)

    def test_empty(self):
        self.assertEqual(index_of([], 1), -1)
