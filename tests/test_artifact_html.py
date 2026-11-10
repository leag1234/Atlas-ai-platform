"""Validation of self-contained HTML artefacts."""

from __future__ import annotations

import time
import unittest

from packages.artifact_html import Problem, validate


class ArtifactHtmlTests(unittest.TestCase):
    def test_accepts_self_contained_document(self) -> None:
        content = """<!doctype html>
<html><body>
<svg xmlns="http://www.w3.org/2000/svg"><text>Chart</text></svg>
<style>body { background: url(data:image/png;base64,AAA) }</style>
<script>const value = 1; console.log(value);</script>
</body></html>"""
        self.assertEqual(validate(content), [])

    def test_reports_text_size_and_document_problems(self) -> None:
        self.assertEqual(validate(" \n"), [Problem("text_empty", 1)])
        self.assertEqual(validate("<p>Body</p>"), [Problem("not_a_document", 1)])
        self.assertEqual(validate("<html><p>Body</p>"), [Problem("not_a_document", 1)])
        self.assertEqual(
            validate("<body><p>Body</p></body>"), [Problem("not_a_document", 1)]
        )
        self.assertEqual(
            validate("<html><body></body></html>" * 20000), [Problem("too_large", 1)]
        )

    def test_reports_url_and_element_problems_with_lines(self) -> None:
        content = """<html><body>
<img src="https://example.test/a.png">
<img src="./local.png">
<img srcset="local.png 1x, data:image/png;base64,AAA 2x">
<iframe src=""></iframe>
<meta http-equiv="refresh" content="1">
<link rel="stylesheet" href="style.css">
<form action="/submit"><button>Send</button></form>
</body></html>"""
        self.assertEqual(
            validate(content),
            [
                Problem("external_reference", 2),
                Problem("relative_reference", 3),
                Problem("relative_reference", 4),
                Problem("relative_reference", 4),
                Problem("forbidden_element", 5),
                Problem("forbidden_element", 6),
                Problem("forbidden_element", 7),
                Problem("relative_reference", 7),
                Problem("form_action", 8),
            ],
        )

    def test_reports_style_and_script_problems_with_lines(self) -> None:
        content = """<html><body>
<div style="background: url(https://example.test/a.png)"></div>
<style>@import "style.css"; @import url(other.css);</style>
<script>
fetch("/api");
window.open("data:text/html,x");
location.href = "https://example.test";
</script>
</body></html>"""
        self.assertEqual(
            validate(content),
            [
                Problem("external_reference", 2),
                Problem("external_reference", 3),
                Problem("external_reference", 3),
                Problem("external_reference", 3),
                Problem("network_api", 5),
                Problem("navigation_api", 6),
                Problem("external_reference", 7),
                Problem("navigation_api", 7),
            ],
        )

    def test_allows_hash_blob_javascript_and_location_hash(self) -> None:
        content = """<html><body>
<a href="#section">Section</a>
<img src="blob:abc">
<a href="javascript:void(0)">Run</a>
<script>location.hash = "#done";</script>
</body></html>"""
        self.assertEqual(validate(content), [])

    def test_allows_svg_and_mathml_namespace_urls_in_scripts(self) -> None:
        content = """<html><body>
<script>
const svg = "http://www.w3.org/2000/svg";
const xhtml = "http://www.w3.org/1999/xhtml";
const xlink = "http://www.w3.org/1999/xlink";
const mathml = "http://www.w3.org/1998/Math/MathML";
const xml = "http://www.w3.org/XML/1998/namespace";
</script>
</body></html>"""
        self.assertEqual(validate(content), [])

    def test_reports_network_and_navigation_api_patterns(self) -> None:
        content = """<html><body>
<script>
const request = new XMLHttpRequest();
const socket = new WebSocket("wss://example.test");
const events = new EventSource("https://example.test");
navigator.sendBeacon("https://example.test");
importScripts("app.js");
import("app.js");
top.location = "https://example.test";
parent.location = "https://example.test";
location.assign("https://example.test");
location.replace("https://example.test");
location.reload();
location = "https://example.test";
</script>
</body></html>"""
        self.assertEqual(
            validate(content),
            [
                Problem("network_api", 3),
                Problem("external_reference", 4),
                Problem("network_api", 4),
                Problem("external_reference", 5),
                Problem("network_api", 5),
                Problem("external_reference", 6),
                Problem("network_api", 6),
                Problem("network_api", 7),
                Problem("network_api", 8),
                Problem("external_reference", 9),
                Problem("navigation_api", 9),
                Problem("external_reference", 10),
                Problem("navigation_api", 10),
                Problem("external_reference", 11),
                Problem("navigation_api", 11),
                Problem("external_reference", 12),
                Problem("navigation_api", 12),
                Problem("navigation_api", 13),
                Problem("external_reference", 14),
                Problem("navigation_api", 14),
            ],
        )

    def test_reports_dialog_api_patterns_with_lines(self) -> None:
        content = """<html><body>
<script>
alert("Game over");
window.confirm("Play again?");
prompt("Name");
</script>
</body></html>"""
        self.assertEqual(
            validate(content),
            [
                Problem("dialog_api", 3),
                Problem("dialog_api", 4),
                Problem("dialog_api", 5),
            ],
        )

    def test_256_kib_document_validates_under_200_ms(self) -> None:
        block = "<p>Atlas self-contained artefact test data.</p>\n"
        content = (
            "<html><body>\n" + (block * 7310)[: 256 * 1024 - 30] + "</body></html>"
        )
        self.assertLessEqual(len(content.encode()), 256 * 1024)
        started = time.perf_counter()
        problems = validate(content)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(problems, [])
        self.assertLess(elapsed_ms, 200)


if __name__ == "__main__":
    unittest.main()


class UnquotedAttributeTests(unittest.TestCase):
    def test_svg_breaking_unquoted_values_are_quoted(self) -> None:
        from packages.artifact_html import quote_attributes

        broken = "<svg viewBox=0 0 10 10 width=40><ellipse cx=5 stroke-width=2/><circle r=1/></svg>"
        self.assertEqual(
            quote_attributes(broken),
            '<svg viewBox="0 0 10 10" width=40><ellipse cx=5 stroke-width="2"/><circle r="1"/></svg>',
        )
        fine = '<svg viewBox="0 0 10 10"><ellipse stroke-width="2"/><br/></svg>'
        self.assertEqual(quote_attributes(fine), fine)
