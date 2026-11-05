import random
import unittest
from collections import OrderedDict

from kvstore import Backend, CachedStore, LRUCache


class Model:
    def __init__(self, cap):
        self.cap, self.d, self.ev = cap, OrderedDict(), 0

    def get(self, k):
        if k in self.d:
            self.d.move_to_end(k)
            return True
        return False

    def put(self, k, v):
        self.d[k] = v
        self.d.move_to_end(k)
        if len(self.d) > self.cap:
            self.d.popitem(last=False)
            self.ev += 1


class HiddenLRU(unittest.TestCase):
    def test_reported_case(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertEqual(c.get("a"), 1)
        c.put("c", 3)
        self.assertIn("a", c)
        self.assertNotIn("b", c)
        self.assertEqual(c.keys(), ["a", "c"])

    def test_put_existing_refreshes(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        c.put("a", 10)
        c.put("c", 3)
        self.assertEqual(c.keys(), ["a", "c"])
        self.assertEqual(c.get("a"), 10)
        self.assertEqual(c.evictions, 1)

    def test_update_existing_at_capacity_no_eviction(self):
        c = LRUCache(1)
        c.put("a", 1)
        c.put("a", 2)
        self.assertEqual(c.evictions, 0)
        self.assertEqual(len(c), 1)

    def test_miss_does_not_change_order(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertIsNone(c.get("zz"))
        self.assertEqual(c.keys(), ["a", "b"])
        self.assertEqual((c.hits, c.misses), (0, 1))

    def test_random_against_model(self):
        rng = random.Random(3)
        for cap in (1, 2, 3, 5):
            c, m = LRUCache(cap), Model(cap)
            for i in range(400):
                k = rng.choice("abcdefg")
                if rng.random() < 0.5:
                    self.assertEqual(c.get(k) is not None, m.get(k))
                else:
                    c.put(k, i)
                    m.put(k, i)
                self.assertEqual(c.keys(), list(m.d))
            self.assertEqual(c.evictions, m.ev)

    def test_store_stats(self):
        store = CachedStore(Backend({"a": 1, "b": 2, "c": 3}), capacity=2)
        store.get("a")
        store.get("b")
        store.get("a")
        store.get("c")
        store.get("a")
        self.assertEqual(
            store.stats(),
            {"hits": 2, "misses": 3, "evictions": 1, "size": 2, "backend_reads": 3},
        )

    def test_capacity_validation(self):
        with self.assertRaises(ValueError):
            LRUCache(0)
