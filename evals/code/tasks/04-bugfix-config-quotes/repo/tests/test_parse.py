import unittest

from confparse import ParseError, parse


class ParseTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse("a = 1\n# c\n\nb=two # note\n"), {"a": "1", "b": "two"})

    def test_missing_equals(self):
        with self.assertRaises(ParseError) as ctx:
            parse("a = 1\nbroken\n")
        self.assertEqual(ctx.exception.lineno, 2)


if __name__ == "__main__":
    unittest.main()
