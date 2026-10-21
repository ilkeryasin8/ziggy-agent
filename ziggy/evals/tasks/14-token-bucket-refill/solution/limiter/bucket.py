"""Token bucket rate limiter."""
import time


class TokenBucket:
    def __init__(self, capacity, refill_rate=1.0, clock=time.monotonic):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._clock = clock
        self.tokens = float(capacity)
        self._last = clock()

    def _refill(self):
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.refill_rate)

    def _check_cost(self, cost):
        if cost <= 0 or cost > self.capacity:
            raise ValueError("cost must be in (0, capacity]")

    def allow(self, cost=1):
        self._check_cost(cost)
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def retry_after(self, cost=1):
        self._check_cost(cost)
        self._refill()
        missing = cost - self.tokens
        if missing <= 0:
            return 0
        return missing / self.refill_rate
