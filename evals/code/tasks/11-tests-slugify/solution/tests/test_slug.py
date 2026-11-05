import unittest

from slug import slugify


class SlugTest(unittest.TestCase):
    def test_accents(self):
        self.assertEqual(slugify("crème brûlée"), "creme-brulee")

    def test_non_ascii_dropped(self):
        self.assertEqual(slugify("a中b"), "ab")

    def test_lowercase(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_runs(self):
        self.assertEqual(slugify("a  --  b"), "a-b")

    def test_edges(self):
        self.assertEqual(slugify("  !hello!  "), "hello")

    def test_custom_sep(self):
        self.assertEqual(slugify("a b c", sep="_"), "a_b_c")
        self.assertEqual(slugify(" a b ", sep="--"), "a--b")

    def test_max_length_word_boundary(self):
        self.assertEqual(slugify("hello world again", max_length=8), "hello")
        self.assertEqual(slugify("hello world", max_length=11), "hello-world")
        self.assertEqual(slugify("supercalifragilistic", max_length=5), "super")

    def test_max_length_no_trailing_sep(self):
        self.assertEqual(slugify("hello world", max_length=6), "hello")

    def test_fallback(self):
        self.assertEqual(slugify("!!!"), "n-a")
        self.assertEqual(slugify("", sep="_"), "n-a")

    def test_bad_sep(self):
        with self.assertRaises(ValueError):
            slugify("a", sep="")
