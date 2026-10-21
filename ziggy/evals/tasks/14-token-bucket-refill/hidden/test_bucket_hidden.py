import unittest

from limiter.bucket import TokenBucket


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class HiddenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_refills_over_time(self):
        b = TokenBucket(3, refill_rate=1.0, clock=self.clock)
        self.assertEqual([b.allow() for _ in range(4)], [True, True, True, False])
        self.clock.advance(1)
        self.assertTrue(b.allow())
        self.assertFalse(b.allow())

    def test_capped_at_capacity(self):
        b = TokenBucket(3, refill_rate=1.0, clock=self.clock)
        for _ in range(3):
            b.allow()
        self.clock.advance(100)
        self.assertEqual([b.allow() for _ in range(4)], [True, True, True, False])

    def test_fractional_accrual(self):
        b = TokenBucket(2, refill_rate=2.0, clock=self.clock)
        b.allow()
        b.allow()
        self.clock.advance(0.25)
        self.assertFalse(b.allow())
        self.clock.advance(0.25)
        self.assertTrue(b.allow())

    def test_failed_allow_consumes_nothing(self):
        b = TokenBucket(2, refill_rate=1.0, clock=self.clock)
        b.allow()
        b.allow()
        self.clock.advance(1.5)
        self.assertFalse(b.allow(cost=2))
        self.clock.advance(0.5)
        self.assertTrue(b.allow(cost=2))

    def test_cost_validation(self):
        b = TokenBucket(2, clock=self.clock)
        for bad in (0, -1, 3):
            with self.assertRaises(ValueError):
                b.allow(cost=bad)

    def test_retry_after(self):
        b = TokenBucket(4, refill_rate=2.0, clock=self.clock)
        self.assertEqual(b.retry_after(), 0)
        for _ in range(4):
            b.allow()
        self.assertEqual(b.retry_after(), 0.5)
        self.assertEqual(b.retry_after(cost=3), 1.5)
        self.clock.advance(0.25)
        self.assertEqual(b.retry_after(), 0.25)
        self.assertTrue(b.allow() is False)

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            TokenBucket(0, clock=self.clock)
        with self.assertRaises(ValueError):
            TokenBucket(1, refill_rate=0, clock=self.clock)
