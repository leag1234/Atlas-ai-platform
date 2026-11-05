import unittest

from notes.store import NoteStore


class StoreTest(unittest.TestCase):
    def test_list(self):
        times = iter([3.0, 1.0, 2.0])
        store = NoteStore(lambda: next(times))
        for text in "abc":
            store.add(text)
        self.assertEqual([n.text for n in store.list_notes()], ["b", "c", "a"])


if __name__ == "__main__":
    unittest.main()
