import unittest

import tagger.tags
from tagger import add_tags, normalize


class TagTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize(" Colour "), "color")
        self.assertEqual(normalize("Blue"), "blue")

    def test_defaults_untouched(self):
        self.assertEqual(tagger.tags.DEFAULT_ALIASES, {"colour": "color", "grey": "gray"})

    def test_add_tags(self):
        self.assertEqual(add_tags({"tags": ["Blue", "blue", "GREY"]}), ["blue", "gray"])

    def test_extra(self):
        self.assertEqual(add_tags({"tags": ["red"]}, ["Navy"]), ["navy", "red"])


if __name__ == "__main__":
    unittest.main()
