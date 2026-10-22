"""Image provenance filtering must preserve all non-image Markdown and prose."""

import unittest
from services.orchestrator.image_truth import verified_images


class ImageMarkdownTests(unittest.TestCase):
    def test_malformed_targets_and_plain_brackets_are_unchanged(self) -> None:
        for text in (
            "Before ![engine](missing(the end.\nMore explanation follows.",
            "Wow![note] this",
            "![note][unknown] after",
            "![note] after",
            r"Escaped \![note](untrusted) after",
            "![incomplete",
            "Before ![x](target\\) after",
        ):
            with self.subTest(text=text):
                self.assertEqual(verified_images(text, set()), text)

    def test_images_inside_code_are_literal(self) -> None:
        for text in (
            "Before `![x](untrusted)` after",
            "``a ` ![x](untrusted)`` after",
            "```markdown\n![x](untrusted)\n```\nAfter",
            "~~~markdown\n![x](untrusted)\n~~~\nAfter",
            "```markdown\n![x](untrusted)\nunfinished fence",
        ):
            with self.subTest(text=text):
                self.assertEqual(verified_images(text, set()), text)

    def test_only_complete_unverified_images_are_removed(self) -> None:
        text = (
            'Before ![x](bad(a).png "title") middle ![ok](</images/ok> "title") after'
        )
        self.assertEqual(
            verified_images(text, {"/images/ok"}),
            'Before  middle ![ok](</images/ok> "title") after',
        )
        text = "![x][ref]\n[ref]: /images/ok\nEnd"
        self.assertEqual(verified_images(text, {"/images/ok"}), text)
        self.assertEqual(verified_images(text, set()), "\n[ref]: /images/ok\nEnd")

    def test_chunk_boundaries_preserve_code_and_balanced_targets(self) -> None:
        from services.orchestrator.image_truth import ImageVerifier

        for text in (
            "Before `![x](untrusted)` after",
            "```markdown\n![x](untrusted)\n```\nAfter",
            "~~~markdown\n![x](untrusted)\n~~~\nAfter",
            "Before ![x](bad(a).png) after",
            "Before ![x](bad(unbalanced) after.\nEnd",
            "Wow![note] this",
            "Before ![x](/images/ok) after",
            "``a ` ![x](untrusted)`` after",
            r"Before \![x](bad) after",
        ):
            expected = verified_images(text, {"/images/ok"})
            for split in range(len(text) + 1):
                with self.subTest(text=text, split=split):
                    verifier = ImageVerifier()
                    answer = verifier.feed(text[:split], {"/images/ok"})
                    answer += verifier.feed(text[split:], {"/images/ok"}, final=True)
                    self.assertEqual(answer, expected)

    def test_reference_definition_before_image_survives_chunk_boundaries(self) -> None:
        from services.orchestrator.image_truth import ImageVerifier

        text = "[ref]: /images/ok\n\nBefore ![diagram][ref] after"
        for references in (set(), {"/images/ok"}):
            expected = verified_images(text, references)
            for split in range(len(text) + 1):
                verifier = ImageVerifier()
                answer = verifier.feed(text[:split], references)
                answer += verifier.feed(text[split:], references, final=True)
                self.assertEqual(answer, expected, (references, split))
