import unittest

from pager.paging import paginate, total_pages


class HiddenPagingTests(unittest.TestCase):
    def test_total_pages_rounds_up(self):
        self.assertEqual(total_pages(10, 3), 4)
        self.assertEqual(total_pages(1, 5), 1)
        self.assertEqual(total_pages(0, 5), 0)
        self.assertEqual(total_pages(6, 3), 2)

    def test_last_partial_page(self):
        result = paginate(list(range(10)), 4, 3)
        self.assertEqual(result["items"], [9])
        self.assertFalse(result["has_next"])
        self.assertEqual(result["total_pages"], 4)

    def test_has_next_before_partial_page(self):
        self.assertTrue(paginate(list(range(10)), 3, 3)["has_next"])

    def test_empty(self):
        result = paginate([], 1, 5)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total_pages"], 0)
        self.assertFalse(result["has_next"])

    def test_past_end(self):
        result = paginate([1, 2, 3], 99, 2)
        self.assertEqual(result["items"], [])
        self.assertFalse(result["has_next"])

    def test_invalid_per_page(self):
        with self.assertRaises(ValueError):
            paginate([1], 1, 0)
