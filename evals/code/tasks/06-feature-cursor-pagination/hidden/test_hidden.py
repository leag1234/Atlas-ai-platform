import random
import unittest

from notes.store import NoteStore


def make(times):
    it = iter(times)
    return NoteStore(lambda: next(it))


def collect(store, limit, mutate=None):
    seen, cursor, pages = [], None, 0
    while True:
        items, cursor = store.list_page(limit, cursor)
        pages += 1
        seen.extend(items)
        if mutate:
            mutate(pages)
        if cursor is None:
            return seen, pages
        assert pages < 1000


class HiddenPaging(unittest.TestCase):
    def test_all_once_in_order(self):
        rng = random.Random(1)
        times = [float(rng.randint(0, 5)) for _ in range(37)]
        store = make(times)
        for i in range(37):
            store.add(f"n{i}")
        for limit in (1, 2, 5, 36, 37, 100):
            seen, pages = collect(store, limit)
            self.assertEqual(seen, store.list_notes())
            self.assertEqual(pages, -(-37 // limit))

    def test_empty(self):
        store = make([])
        self.assertEqual(store.list_page(10), ([], None))

    def test_exact_multiple_no_extra_cursor(self):
        store = make([1.0, 2.0, 3.0, 4.0])
        for t in "abcd":
            store.add(t)
        items, cursor = store.list_page(2)
        items, cursor = store.list_page(2, cursor)
        self.assertEqual([n.text for n in items], ["c", "d"])
        self.assertIsNone(cursor)

    def test_limit_validation(self):
        store = make([])
        for bad in (0, -1, 101, 2.5, "3", None):
            with self.assertRaises((ValueError, TypeError)):
                store.list_page(bad)
        with self.assertRaises(ValueError):
            store.list_page(0)
        with self.assertRaises(ValueError):
            store.list_page(101)

    def test_bad_cursor(self):
        store = make([1.0, 2.0, 3.0])
        for t in "abc":
            store.add(t)
        _, cursor = store.list_page(1)
        for bad in ("", "garbage", "!!!", cursor[:-1] + ("A" if cursor[-1] != "A" else "B"), "null", "[]"):
            if bad == cursor:
                continue
            with self.assertRaises(ValueError, msg=bad):
                store.list_page(1, bad)

    def test_deletes_during_paging(self):
        rng = random.Random(2)
        times = [float(rng.randint(0, 9)) for _ in range(60)]
        store = make(times)
        notes = [store.add(str(i)) for i in range(60)]
        ordered = store.list_notes()
        deleted = set()
        seen = []
        cursor = None
        while True:
            items, cursor = store.list_page(7, cursor)
            seen.extend(items)
            for n in rng.sample(notes, 3):
                store.delete(n.id)
                deleted.add(n.id)
            if cursor is None:
                break
        ids = [n.id for n in seen]
        self.assertEqual(len(ids), len(set(ids)))
        expected = [n.id for n in ordered if n.id not in deleted or n.id in ids]
        survivors = [n.id for n in ordered if n.id not in deleted]
        for sid in survivors:
            self.assertIn(sid, ids)
        self.assertEqual(ids, [i for i in [n.id for n in ordered] if i in set(ids)])

    def test_inserts_during_paging(self):
        times = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 0.5, 9.0, 0.1, 10.0]
        store = make(times)
        original = [store.add(str(i)) for i in range(6)]
        extra = iter(range(6, 10))

        def mutate(page):
            try:
                store.add("x" + str(next(extra)))
            except StopIteration:
                pass

        seen, _ = collect(store, 2, mutate)
        ids = [n.id for n in seen]
        self.assertEqual(len(ids), len(set(ids)))
        for n in original:
            self.assertIn(n.id, ids)
