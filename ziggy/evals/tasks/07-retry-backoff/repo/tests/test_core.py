import unittest

from retry.core import retry


class RetryTests(unittest.TestCase):
    def test_succeeds_first_time(self):
        self.assertEqual(retry(lambda: 42), 42)

    def test_eventually_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("nope")
            return "ok"

        self.assertEqual(retry(flaky, 3), "ok")
        self.assertEqual(len(calls), 3)

    def test_raises_last_error(self):
        def always():
            raise KeyError("boom")

        with self.assertRaises(KeyError):
            retry(always, attempts=1)
