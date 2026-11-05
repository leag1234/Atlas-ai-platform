"""Store with a cache in front of a backend."""

from kvstore.cache import LRUCache

_MISSING = object()


class Backend:
    """In-memory stand-in for a slow backend; counts reads."""

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.reads = 0

    def read(self, key):
        self.reads += 1
        return self.data[key]

    def write(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class CachedStore:
    def __init__(self, backend, capacity=128):
        self.backend = backend
        self.cache = LRUCache(capacity)

    def get(self, key):
        value = self.cache.get(key, _MISSING)
        if value is _MISSING:
            value = self.backend.read(key)
            self.cache.put(key, value)
        return value

    def set(self, key, value):
        self.backend.write(key, value)
        self.cache.put(key, value)

    def delete(self, key):
        self.backend.delete(key)
        self.cache.pop(key)

    def stats(self):
        return {
            "hits": self.cache.hits,
            "misses": self.cache.misses,
            "evictions": self.cache.evictions,
            "size": len(self.cache),
            "backend_reads": self.backend.reads,
        }
