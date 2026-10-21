import unittest

from retry.core import retry


def failing(times, exc=RuntimeError):
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= times:
            raise exc("fail %d" % state["n"])
        return state["n"]

    fn.state = state
    return fn


class HiddenRetryTests(unittest.TestCase):
    def test_exponential_delays(self):
        sleeps = []
        fn = failing(3)
        result = retry(fn, attempts=5, base_delay=0.5, sleep=sleeps.append)
        self.assertEqual(result, 4)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])

    def test_no_sleep_after_last_attempt(self):
        sleeps = []
        fn = failing(10)
        with self.assertRaises(RuntimeError) as ctx:
            retry(fn, attempts=3, base_delay=1, sleep=sleeps.append)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(str(ctx.exception), "fail 3")
        self.assertEqual(fn.state["n"], 3)

    def test_max_delay_cap(self):
        sleeps = []
        retry(failing(4), attempts=6, base_delay=1, factor=3, max_delay=5, sleep=sleeps.append)
        self.assertEqual(sleeps, [1, 3, 5, 5])

    def test_retry_on_filters(self):
        sleeps = []
        fn = failing(2, exc=KeyError)
        with self.assertRaises(KeyError):
            retry(fn, attempts=5, retry_on=(ValueError,), sleep=sleeps.append)
        self.assertEqual(sleeps, [])
        self.assertEqual(fn.state["n"], 1)

    def test_retry_on_matches(self):
        sleeps = []
        result = retry(failing(1, exc=ValueError), attempts=3, retry_on=(ValueError,),
                       sleep=sleeps.append)
        self.assertEqual(result, 2)
        self.assertEqual(len(sleeps), 1)

    def test_invalid_attempts(self):
        with self.assertRaises(ValueError):
            retry(lambda: 1, attempts=0)

    def test_single_attempt_never_sleeps(self):
        sleeps = []
        with self.assertRaises(RuntimeError):
            retry(failing(1), attempts=1, sleep=sleeps.append)
        self.assertEqual(sleeps, [])
