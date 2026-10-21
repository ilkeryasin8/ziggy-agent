import unittest

from cache.store import Cache


class CacheTests(unittest.TestCase):
    def test_set_get(self):
        c = Cache()
        c.set("a", 1)
        self.assertEqual(c.get("a"), 1)
        self.assertEqual(c.get("b", "dflt"), "dflt")

    def test_len_and_contains(self):
        c = Cache()
        c.set("a", 1)
        c.set("a", 2)
        self.assertEqual(len(c), 1)
        self.assertIn("a", c)
        self.assertNotIn("z", c)
