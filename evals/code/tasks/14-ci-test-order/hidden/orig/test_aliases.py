import unittest

from tagger import add_tags, load_aliases, normalize


class AliasTest(unittest.TestCase):
    def test_custom_alias(self):
        load_aliases({"Tee": "t-shirt"})
        self.assertEqual(normalize(" TEE "), "t-shirt")
        self.assertEqual(add_tags({"tags": ["tee", "Grey"]}), ["gray", "t-shirt"])


if __name__ == "__main__":
    unittest.main()
