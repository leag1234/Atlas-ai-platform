import unittest

from confparse import ParseError, parse


class HiddenParse(unittest.TestCase):
    def test_reported(self):
        line = 'motd = "Welcome to #general \\"team\\""   # shown at login\n'
        self.assertEqual(parse(line), {"motd": 'Welcome to #general "team"'})

    def test_escapes(self):
        self.assertEqual(parse('p = "C:\\\\dir\\\\x"'), {"p": "C:\\dir\\x"})
        self.assertEqual(parse('m = "a\\nb"'), {"m": "a\nb"})
        self.assertEqual(parse('e = ""'), {"e": ""})
        self.assertEqual(parse('s = "  padded  "'), {"s": "  padded  "})

    def test_unquoted(self):
        self.assertEqual(parse("a = x y z   # c"), {"a": "x y z"})
        self.assertEqual(parse("empty =\n"), {"empty": ""})
        self.assertEqual(parse("eq = a=b"), {"eq": "a=b"})
        self.assertEqual(parse('mid = ab"cd'), {"mid": 'ab"cd'})

    def test_last_wins(self):
        self.assertEqual(parse("a=1\na=2"), {"a": "2"})

    def test_comment_lines(self):
        self.assertEqual(parse("   # only comment\n\t\n"), {})

    def assertError(self, text, lineno):
        with self.assertRaises(ParseError) as ctx:
            parse(text)
        self.assertEqual(ctx.exception.lineno, lineno)

    def test_errors(self):
        self.assertError('ok = 1\nbad = "open\n', 2)
        self.assertError('a = "x" trailing', 1)
        self.assertError('a = "bad \\q escape"', 1)
        self.assertError("\n\n = value", 3)
        self.assertError("x\n", 1)
        self.assertError('a = "ends with backslash\\"', 1)
        self.assertTrue(issubclass(ParseError, ValueError))
