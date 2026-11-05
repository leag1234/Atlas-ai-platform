import unittest

from rangeset import merge_intervals, total_length


class MergeTest(unittest.TestCase):
    def test_disjoint(self):
        self.assertEqual(merge_intervals([(1, 2), (4, 6)]), [(1, 2), (4, 6)])

    def test_overlap_sorted(self):
        self.assertEqual(merge_intervals([(1, 4), (2, 6)]), [(1, 6)])

    def test_total_length(self):
        self.assertEqual(total_length([(0, 10), (20, 25)]), 15)


if __name__ == "__main__":
    unittest.main()
