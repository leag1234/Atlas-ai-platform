import importlib
import pathlib
import re
import subprocess
import sys
import unittest
import warnings

PAIRS = {
    "textkit.case": [("toSnake", "to_snake")],
    "textkit.words": [("wordCount", "word_count"), ("truncateWords", "truncate_words")],
    "textkit.normalize": [("stripAccents", "strip_accents")],
}
ARGS = {"toSnake": ("Crème BrûléeRecipe",), "wordCount": ("it's a test",), "stripAccents": ("Ångström",), "truncateWords": ("a b c d e", 3)}


class HiddenRename(unittest.TestCase):
    def test_new_names_no_warning(self):
        import textkit
        for mod, pairs in PAIRS.items():
            m = importlib.import_module(mod)
            for old, new in pairs:
                with warnings.catch_warnings():
                    warnings.simplefilter("error")
                    getattr(m, new)(*ARGS[old])
                    getattr(textkit, new)(*ARGS[old])

    def test_old_names_warn_and_match(self):
        import textkit
        for mod, pairs in PAIRS.items():
            m = importlib.import_module(mod)
            for old, new in pairs:
                for owner in (m, textkit):
                    for _ in range(2):
                        with warnings.catch_warnings(record=True) as caught:
                            warnings.simplefilter("always")
                            result = getattr(owner, old)(*ARGS[old])
                        self.assertEqual(result, getattr(m, new)(*ARGS[old]))
                        deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
                        self.assertEqual(len(deps), 1, (owner, old))
                        self.assertIn(new, str(deps[0].message))

    def test_behaviour(self):
        from textkit import to_snake, truncate_words, word_count, strip_accents
        self.assertEqual(to_snake("parseHTTPResponse2Fast"), "parse_httpresponse2_fast")
        self.assertEqual(truncate_words("a b c d", 2, suffix="~"), "a b~")
        self.assertEqual(word_count(""), 0)
        self.assertEqual(strip_accents("façade"), "facade")

    def test_cli_clean(self):
        for cmd, expected in (("snake", "hello_world\n"), ("count", "2\n"), ("summary", "2 words: Hello World\n")):
            r = subprocess.run([sys.executable, "-W", "error::DeprecationWarning", "-m", "textkit.cli", cmd, "Hello", "World"],
                               cwd="/work", capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (0, expected), r.stderr)

    def test_no_old_calls(self):
        old = re.compile(r"\b(toSnake|wordCount|stripAccents|truncateWords)\s*\(")
        for path in list(pathlib.Path("/work/textkit").rglob("*.py")) + list(pathlib.Path("/work/tests").rglob("*.py")):
            for line in path.read_text().splitlines():
                if old.search(line) and not line.lstrip().startswith("def "):
                    self.fail(f"{path}: {line.strip()}")

    def test_old_tests_run_without_warnings(self):
        r = subprocess.run([sys.executable, "-W", "error::DeprecationWarning", "-m", "unittest", "discover", "-s", "tests", "-q"],
                           cwd="/work", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
