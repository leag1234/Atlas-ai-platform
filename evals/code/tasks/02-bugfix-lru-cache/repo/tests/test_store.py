import unittest

from kvstore import Backend, CachedStore


class StoreTest(unittest.TestCase):
    def test_read_through(self):
        store = CachedStore(Backend({"a": 1}), capacity=2)
        self.assertEqual(store.get("a"), 1)
        self.assertEqual(store.get("a"), 1)
        self.assertEqual(store.stats()["backend_reads"], 1)

    def test_delete(self):
        store = CachedStore(Backend({"a": 1}), capacity=2)
        store.get("a")
        store.delete("a")
        with self.assertRaises(KeyError):
            store.get("a")


if __name__ == "__main__":
    unittest.main()
