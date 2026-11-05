import unittest

from textkit import strip_accents, to_snake, truncate_words, word_count


class TextkitTest(unittest.TestCase):
    def test_snake(self):
        self.assertEqual(to_snake("Crème Brûlée Recipe"), "creme_brulee_recipe")
        self.assertEqual(to_snake("parseHTTPResponse2Fast"), "parse_httpresponse2_fast")

    def test_words(self):
        self.assertEqual(word_count("it's a test, isn't it"), 5)
        self.assertEqual(truncate_words("a b c d", 2), "a b...")

    def test_accents(self):
        self.assertEqual(strip_accents("Ångström"), "Angstrom")


if __name__ == "__main__":
    unittest.main()
