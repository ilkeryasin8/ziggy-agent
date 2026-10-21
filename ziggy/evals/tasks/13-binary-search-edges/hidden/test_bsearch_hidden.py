import unittest

from algos.bsearch import index_of


class HiddenBsearchTests(unittest.TestCase):
    def test_single_element(self):
        self.assertEqual(index_of([4], 4), 0)
        self.assertEqual(index_of([4], 5), -1)
        self.assertEqual(index_of([4], 3), -1)

    def test_every_position_every_size(self):
        for n in range(1, 25):
            data = [i * 2 for i in range(n)]
            for i, value in enumerate(data):
                self.assertEqual(index_of(data, value), i, (n, i))

    def test_absent_values_every_size(self):
        for n in range(0, 25):
            data = [i * 2 for i in range(n)]
            for probe in range(-1, 2 * n + 2, 2):
                self.assertEqual(index_of(data, probe), -1, (n, probe))

    def test_leftmost_duplicate(self):
        self.assertEqual(index_of([1, 2, 2, 2, 2, 3], 2), 1)
        self.assertEqual(index_of([7, 7, 7, 7], 7), 0)
        self.assertEqual(index_of([1, 1, 2, 3, 3, 3], 3), 3)

    def test_input_not_modified(self):
        data = [1, 2, 3]
        index_of(data, 3)
        self.assertEqual(data, [1, 2, 3])

    def test_strings(self):
        self.assertEqual(index_of(["a", "b", "c"], "c"), 2)
