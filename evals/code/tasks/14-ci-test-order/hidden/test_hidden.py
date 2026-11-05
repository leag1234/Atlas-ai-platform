import unittest

import tagger.tags as tags
from tagger import add_tags, load_aliases, normalize


class HiddenState(unittest.TestCase):
    def setUp(self):
        load_aliases({})

    def test_independent_calls(self):
        self.assertEqual(add_tags({"tags": ["a"]}), ["a"])
        self.assertEqual(add_tags({"tags": ["b"]}), ["b"])
        self.assertEqual(add_tags({}), [])

    def test_inputs_unchanged(self):
        record = {"tags": ["X", "y"]}
        extra = ["Z"]
        self.assertEqual(add_tags(record, extra), ["x", "y", "z"])
        self.assertEqual((record, extra), ({"tags": ["X", "y"]}, ["Z"]))

    def test_replace_semantics(self):
        load_aliases({"a": "b"})
        self.assertEqual(normalize("a"), "b")
        load_aliases({"c": "d"})
        self.assertEqual(normalize("a"), "a")
        self.assertEqual(normalize("c"), "d")
        self.assertEqual(normalize("grey"), "gray")
        self.assertEqual(tags.DEFAULT_ALIASES, {"colour": "color", "grey": "gray"})

    def test_override_default(self):
        load_aliases({"grey": "silver"})
        self.assertEqual(normalize("grey"), "silver")
        load_aliases({})
        self.assertEqual(normalize("grey"), "gray")
