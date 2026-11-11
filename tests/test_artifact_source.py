"""The app or drawing file is read from a fenced block of the model's answer."""

import shutil
import unittest

from packages.artifact_html import validate
from packages.artifact_source import (
    DRAWING_MARKER,
    LineCounter,
    blocks,
    drawing_document,
    drawing_source,
    filename_for,
    is_drawing,
    last_block,
    prepare,
    prose,
    svg_problems,
    title_of,
)

HTML = (
    '<!DOCTYPE html>\n<html lang="fr"><head><title>Minuteur</title></head>\n'
    "<body><button>Start</button><script>let n = 0;</script></body></html>"
)
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>Chat</title><circle cx="5" cy="5" r="4"/></svg>'


class BlockTests(unittest.TestCase):
    def test_last_complete_block_wins_and_unterminated_is_ignored(self) -> None:
        text = (
            "Draft:\n```html\n<html><body>old</body></html>\n```\n"
            f"Final:\n```html\n{HTML}\n```\nUse the big button.\n"
            "```html\n<html><body>cut"
        )
        block = last_block(text)
        assert block is not None
        self.assertEqual((block.kind, block.content), ("html", HTML))
        self.assertFalse(blocks(text)[-1].complete)

    def test_kind_from_language_or_content(self) -> None:
        self.assertEqual(last_block(f"```svg\n{SVG}\n```").kind, "svg")  # type: ignore[union-attr]
        self.assertEqual(last_block(f"```xml\n{SVG}\n```").kind, "svg")  # type: ignore[union-attr]
        self.assertEqual(last_block(f"```\n{HTML}\n```").kind, "html")  # type: ignore[union-attr]
        self.assertIsNone(last_block("```python\nprint(1)\n```"))
        self.assertIsNone(last_block("```javascript\nlet a = 1;\n```"))

    def test_longer_fences_and_inner_backticks(self) -> None:
        inner = "<html><body><script>const t = `a ${1}`;</script></body></html>"
        text = f"````html\n{inner}\n```\nnot the end\n````\nDone."
        block = last_block(text)
        assert block is not None
        self.assertEqual(block.content, inner + "\n```\nnot the end")

    def test_unfenced_document_is_a_fallback(self) -> None:
        block = last_block(f"Voici l'application :\n{HTML}\nBonne utilisation.")
        assert block is not None
        self.assertEqual((block.kind, block.content), ("html", HTML))
        drawing = last_block(f"Here: {SVG}")
        assert drawing is not None
        self.assertEqual(drawing.kind, "svg")
        # A cut fenced block is never completed from its raw text.
        self.assertIsNone(last_block("```html\n" + HTML))

    def test_prose_drops_every_block(self) -> None:
        text = f"Intro.\n```html\n{HTML}\n```\n\n\n\nClick start.\n```js\nx\n```"
        self.assertEqual(prose(text), "Intro.\n\nClick start.")
        self.assertEqual(prose(f"Here {HTML} enjoy"), "Here  enjoy")


class LineCounterTests(unittest.TestCase):
    def test_counts_lines_inside_the_fence_across_chunks(self) -> None:
        counter = LineCounter()
        self.assertEqual(counter.feed("Sure.\n``"), 0)
        self.assertFalse(counter.inside)
        counter.feed("`html\n<html>\n<bo")
        self.assertTrue(counter.inside)
        self.assertEqual(counter.feed("dy>\n</body>\n"), 3)
        counter.feed("</html>\n```\nDone")
        self.assertFalse(counter.inside)
        self.assertEqual(counter.lines, 4)


class FileTests(unittest.TestCase):
    def test_title_and_filename(self) -> None:
        self.assertEqual(title_of(HTML), "Minuteur")
        self.assertEqual(
            title_of("<title>Boîte &amp; [rythme]</title>"), "Boîte & rythme"
        )
        self.assertEqual(filename_for("Boîte à rythme !", "app"), "boite-a-rythme.html")
        self.assertEqual(filename_for("日本", "drawing"), "drawing.html")
        self.assertRegex(
            filename_for("x" * 200, "app"), r"^[a-z0-9][a-z0-9-]{0,60}\.html$"
        )

    def test_svg_parse_repairs_ampersands_and_refuses_entities(self) -> None:
        svg, problems = svg_problems(
            '<svg viewBox="0 0 10 10"><text>Tom & Jerry</text></svg>'
        )
        self.assertEqual(problems, [])
        self.assertIn("Tom &amp; Jerry", svg)
        _, problems = svg_problems('<!DOCTYPE svg [<!ENTITY a "b">]><svg>&a;</svg>')
        self.assertEqual([p.code for p in problems], ["svg_doctype"])
        _, problems = svg_problems(
            '<svg viewBox="0 0 10 10">\n<g>\n<circle r="1">\n</svg>'
        )
        self.assertEqual([p.code for p in problems], ["svg_syntax"])
        self.assertGreater(problems[0].line, 1)
        _, problems = svg_problems("<html><body></body></html>")
        self.assertEqual([p.code for p in problems], ["svg_root"])

    def test_unquoted_attributes_are_quoted_before_parsing(self) -> None:
        _, problems = svg_problems(
            '<svg viewBox=0 0 10 10><line x1="0" stroke-width=2/></svg>'
        )
        self.assertEqual(problems, [])

    def test_drawing_document_is_a_valid_page(self) -> None:
        document = drawing_document(
            '<svg width="200" height="100"><rect width="1" height="1"/></svg>',
            "Chat <bleu>",
            "fr",
        )
        self.assertEqual(validate(document), [])
        self.assertIn(DRAWING_MARKER, document)
        self.assertIn('viewBox="0 0 200 100"', document)
        self.assertIn("<title>Chat &lt;bleu&gt;</title>", document)
        self.assertTrue(is_drawing(document))
        self.assertFalse(is_drawing(HTML))
        self.assertTrue(drawing_source(document).startswith("<svg"))

    def test_prepare(self) -> None:
        drawing = last_block(f"```svg\n{SVG}\n```")
        assert drawing is not None
        prepared = prepare(drawing, "", "en")
        self.assertEqual(prepared.problems, [])
        self.assertIn("<title>Chat</title>", prepared.document)

    @unittest.skipUnless(shutil.which("node"), "Node.js parses the scripts")
    def test_prepare_reports_script_syntax(self) -> None:
        broken = last_block(
            "```html\n<!DOCTYPE html><html><body>\n<script>if (x {</script>"
            "</body></html>\n```"
        )
        assert broken is not None
        prepared = prepare(broken, "x", "en")
        self.assertEqual([p.code for p in prepared.problems], ["script_syntax"])
        self.assertEqual(prepared.source, prepared.document)


if __name__ == "__main__":
    unittest.main()
