"""Retry helper."""
import time


def retry(fn, attempts=3, base_delay=0.1, factor=2.0, max_delay=None,
          retry_on=(Exception,), sleep=time.sleep):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for n in range(attempts):
        try:
            return fn()
        except retry_on:
            if n == attempts - 1:
                raise
            delay = base_delay * factor ** n
            if max_delay is not None:
                delay = min(delay, max_delay)
            sleep(delay)
