import unittest

from cache.store import Cache


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class HiddenCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_expires_at_boundary(self):
        c = Cache(clock=self.clock)
        c.set("a", 1, ttl=10)
        self.clock.now = 109.9
        self.assertEqual(c.get("a"), 1)
        self.clock.now = 110.0
        self.assertIsNone(c.get("a"))
        self.assertNotIn("a", c)

    def test_default_ttl_and_override(self):
        c = Cache(default_ttl=5, clock=self.clock)
        c.set("short", 1)
        c.set("long", 2, ttl=50)
        self.clock.now = 106
        self.assertEqual(c.get("short", "gone"), "gone")
        self.assertEqual(c.get("long"), 2)

    def test_no_ttl_never_expires(self):
        c = Cache(clock=self.clock)
        c.set("a", 1)
        self.clock.now = 1e9
        self.assertEqual(c.get("a"), 1)

    def test_overwrite_restarts_lifetime(self):
        c = Cache(clock=self.clock)
        c.set("a", 1, ttl=10)
        self.clock.now = 108
        c.set("a", 2, ttl=10)
        self.clock.now = 115
        self.assertEqual(c.get("a"), 2)
        self.clock.now = 118
        self.assertIsNone(c.get("a"))

    def test_len_counts_live_only(self):
        c = Cache(clock=self.clock)
        c.set("a", 1, ttl=1)
        c.set("b", 2, ttl=100)
        c.set("c", 3)
        self.clock.now = 102
        self.assertEqual(len(c), 2)

    def test_purge_returns_count(self):
        c = Cache(clock=self.clock)
        c.set("a", 1, ttl=1)
        c.set("b", 2, ttl=2)
        c.set("c", 3, ttl=100)
        self.clock.now = 103
        self.assertEqual(c.purge(), 2)
        self.assertEqual(c.purge(), 0)
        self.assertEqual(len(c), 1)

    def test_invalid_ttl(self):
        c = Cache(clock=self.clock)
        with self.assertRaises(ValueError):
            c.set("a", 1, ttl=0)
        with self.assertRaises(ValueError):
            c.set("a", 1, ttl=-3)
