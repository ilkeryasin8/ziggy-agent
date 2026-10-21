import unittest

from limiter.bucket import TokenBucket


class BucketTests(unittest.TestCase):
    def test_exhausts(self):
        bucket = TokenBucket(2)
        self.assertTrue(bucket.allow())
        self.assertTrue(bucket.allow())
        self.assertFalse(bucket.allow())

    def test_single_token(self):
        bucket = TokenBucket(1)
        self.assertTrue(bucket.allow())
        self.assertFalse(bucket.allow())
