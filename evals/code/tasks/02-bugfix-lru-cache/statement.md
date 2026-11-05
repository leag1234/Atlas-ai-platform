`kvstore.CachedStore` is supposed to keep the most recently used keys in memory, but operators see
cache misses on keys that are read constantly: with capacity 2, reading `a`, then writing `c`, evicts `a`
instead of `b`.

Fix the cache so it is a true least-recently-used cache, as described in the docstrings of
`kvstore/cache.py`. Both a successful `get` and a `put` (new or existing key) count as a use. The
statistics exposed by `CachedStore.stats()` must stay correct.
