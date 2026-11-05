import random
import unittest

from rangeset import merge_intervals, total_length


def reference(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


class HiddenMerge(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(merge_intervals([(5, 7), (1, 3), (2, 4)]), [(1, 4), (5, 7)])
        self.assertEqual(merge_intervals([(1, 3), (3, 5)]), [(1, 5)])
        self.assertEqual(merge_intervals([]), [])
        self.assertEqual(merge_intervals([(2, 2)]), [(2, 2)])
        self.assertEqual(merge_intervals([(1, 10), (2, 3), (4, 5)]), [(1, 10)])
        self.assertEqual(merge_intervals([(-5, -1), (-1, 0)]), [(-5, 0)])

    def test_not_modified_and_new_list(self):
        data = [(3, 4), (1, 2)]
        out = merge_intervals(data)
        self.assertEqual(data, [(3, 4), (1, 2)])
        self.assertIsNot(out, data)
        self.assertTrue(all(isinstance(x, tuple) for x in out))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            merge_intervals([(1, 2), (5, 4)])

    def test_random(self):
        rng = random.Random(7)
        for _ in range(500):
            data = []
            for _ in range(rng.randint(0, 8)):
                s = rng.randint(-20, 20)
                data.append((s, s + rng.randint(0, 6)))
            self.assertEqual(merge_intervals(data), reference(data), data)
            self.assertEqual(total_length(data), sum(e - s for s, e in reference(data)))

    def test_generator_input(self):
        self.assertEqual(merge_intervals(iter([(4, 5), (1, 4)])), [(1, 5)])
