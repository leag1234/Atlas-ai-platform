"""A small least-recently-used cache."""

from collections import OrderedDict


class LRUCache:
    """Least-recently-used cache holding at most ``capacity`` entries.

    A successful ``get`` and any ``put`` mark the key as most recently used.
    When a ``put`` adds a new key beyond capacity, the least recently used key
    is evicted. ``hits`` and ``misses`` count ``get`` results; ``evictions``
    counts evicted entries.
    """

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._data = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key, default=None):
        if key in self._data:
            self.hits += 1
            self._data.move_to_end(key)
            return self._data[key]
        self.misses += 1
        return default

    def put(self, key, value):
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)
            self.evictions += 1

    def pop(self, key):
        """Remove ``key`` if present; not counted as a use or an eviction."""
        self._data.pop(key, None)

    def keys(self):
        """Keys from least to most recently used."""
        return list(self._data)

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)
