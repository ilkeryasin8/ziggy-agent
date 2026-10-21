"""Token bucket rate limiter."""


class TokenBucket:
    def __init__(self, capacity):
        self.capacity = capacity
        self.tokens = capacity

    def allow(self):
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False
