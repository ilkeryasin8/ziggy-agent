import unittest

from pager.paging import paginate


class PaginateTests(unittest.TestCase):
    def test_exact_multiple(self):
        result = paginate(list(range(9)), 2, 3)
        self.assertEqual(result["items"], [3, 4, 5])
        self.assertEqual(result["total_pages"], 3)
        self.assertTrue(result["has_next"])

    def test_invalid_page(self):
        with self.assertRaises(ValueError):
            paginate([1], 0, 3)
