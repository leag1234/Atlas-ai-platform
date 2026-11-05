import unittest

from textkit import stripAccents, toSnake, truncateWords, wordCount


class TextkitTest(unittest.TestCase):
    def test_snake(self):
        self.assertEqual(toSnake("Crème Brûlée Recipe"), "creme_brulee_recipe")
        self.assertEqual(toSnake("parseHTTPResponse2Fast"), "parse_httpresponse2_fast")

    def test_words(self):
        self.assertEqual(wordCount("it's a test, isn't it"), 5)
        self.assertEqual(truncateWords("a b c d", 2), "a b...")

    def test_accents(self):
        self.assertEqual(stripAccents("Ångström"), "Angstrom")


if __name__ == "__main__":
    unittest.main()
