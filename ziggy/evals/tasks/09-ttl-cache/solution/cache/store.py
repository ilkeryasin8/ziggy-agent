"""In-memory cache with optional expiry."""
import time


class Cache:
    def __init__(self, default_ttl=None, clock=time.monotonic):
        self._data = {}
        self._default_ttl = default_ttl
        self._clock = clock

    def set(self, key, value, ttl=None):
        if ttl is None:
            ttl = self._default_ttl
        if ttl is None:
            expires = None
        elif ttl <= 0:
            raise ValueError("ttl must be positive")
        else:
            expires = self._clock() + ttl
        self._data[key] = (value, expires)

    def _live(self, key):
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry[1] is not None and self._clock() >= entry[1]:
            del self._data[key]
            return None
        return entry

    def get(self, key, default=None):
        entry = self._live(key)
        return default if entry is None else entry[0]

    def purge(self):
        now = self._clock()
        dead = [k for k, (_, exp) in self._data.items() if exp is not None and now >= exp]
        for key in dead:
            del self._data[key]
        return len(dead)

    def __contains__(self, key):
        return self._live(key) is not None

    def __len__(self):
        self.purge()
        return len(self._data)
