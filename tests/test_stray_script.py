"""A stray character of another script glued to a word of a Latin-script answer.

Fictional content only. The defect: a French study read "scientifiquement"
followed by a Han character, then "active" (staging, 2026-11-19).
"""

import asyncio
import unittest

from services.orchestrator.stray_script import (
    Gate,
    Rewriter,
    clean,
    latin_language,
    script,
    scripts_in,
)

HAN = "活"  # a Han character
KANJI_CITY = "東京"  # two Han characters, a city name
CYRILLIC_A = "а"
CYRILLIC_WORD = "Москва"
ARABIC = "ق"
ARABIC_WORD = "القاهرة"
GREEK_MU = "μ"


class CleanTests(unittest.TestCase):
    def test_the_staging_defect_is_removed(self) -> None:
        text = f"La piste est documentee comme scientifiquement{HAN} active en 2026."
        cleaned, counts = clean(text, "fr")
        self.assertEqual(
            cleaned, "La piste est documentee comme scientifiquement active en 2026."
        )
        self.assertEqual(counts, {"removed": 1, "cjk": 1})

    def test_glued_characters_of_other_scripts_are_removed(self) -> None:
        cases = {
            f"Le port de{ARABIC} Velmora": "Le port de Velmora",
            f"The {HAN}valley is green.": "The valley is green.",
            f"Die Stadt Orlen{HAN}{HAN} liegt am Fluss.": (
                "Die Stadt Orlen liegt am Fluss."
            ),
            "El mapa de Ternal\u05d0 es antiguo.": "El mapa de Ternal es antiguo.",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(clean(text, "fr")[0], expected)

    def test_a_cyrillic_twin_inside_a_latin_word_becomes_its_latin_letter(
        self,
    ) -> None:
        cleaned, counts = clean(f"La ville de V{CYRILLIC_A}lmora.", "fr")
        self.assertEqual(cleaned, "La ville de Valmora.")
        self.assertEqual(counts, {"replaced": 1, "cyrillic": 1})

    def test_legitimate_uses_are_kept(self) -> None:
        kept = [
            # A name in its own script, in parentheses or quotation marks.
            f"Tokyo ({KANJI_CITY}) est une capitale.",
            f"Le nom «{HAN}» se lit huo.",
            f'The sign reads "{HAN}" in the old town.',
            f"Le panneau dit “Entree{HAN}”.",
            # A run of that script: a word written in it.
            f"Moscou se dit {CYRILLIC_WORD} en russe.",
            f"Le Caire, {ARABIC_WORD}, est au bord du Nil.",
            f"Le quartier Nishi{KANJI_CITY}{HAN} est fictif.",
            # Alone between spaces.
            f"Le caractere {HAN} signifie vivre.",
            # Code, inline code, links.
            f"Run `echo {HAN}x` to test.",
            f"```python\nlabel = 'name{HAN}'\n```\nDone.",
            f"See https://example.org/wiki/page{HAN}x for more.",
            f"Voir [la page](https://example.org/a{HAN}b).",
            # Greek letters of Latin-script science.
            f"Le signal dure 5 {GREEK_MU}s et 3{GREEK_MU}m.",
            # After a digit, not a Latin letter.
            f"En 2025{HAN} rien.",
        ]
        for text in kept:
            with self.subTest(text=text):
                self.assertEqual(clean(text, "fr"), (text, {}))

    def test_the_sources_block_is_kept_as_published(self) -> None:
        text = (
            f"Une reponse{HAN} courte.\n\n<details>\n<summary>Sources</summary>\n"
            f"- Guide{HAN}x\n</details>"
        )
        cleaned, counts = clean(text, "fr")
        self.assertEqual(
            cleaned,
            "Une reponse courte.\n\n<details>\n<summary>Sources</summary>\n"
            f"- Guide{HAN}x\n</details>",
        )
        self.assertEqual(counts["removed"], 1)

    def test_a_script_the_person_used_is_kept(self) -> None:
        asked = scripts_in([f"Comment lit-on {HAN} et {CYRILLIC_WORD} ?"])
        self.assertEqual(asked, frozenset({"cjk", "cyrillic"}))
        text = f"On lit{HAN} huo, et V{CYRILLIC_A}lmora reste ainsi."
        self.assertEqual(clean(text, "fr", asked), (text, {}))
        # Another script is still checked.
        self.assertEqual(
            clean(f"Le port de{ARABIC} Velmora", "fr", asked)[0], "Le port de Velmora"
        )

    def test_an_answer_in_another_script_is_not_checked(self) -> None:
        for language in ("zh-cn", "ja", "ru", "ar", "el", "ko"):
            with self.subTest(language=language):
                self.assertFalse(latin_language(language))
                text = f"Tokyo{KANJI_CITY[0]} abc{CYRILLIC_A}def"
                self.assertEqual(clean(text, language), (text, {}))
        for language in ("fr", "en", "de", "es", "it", "pt", "pt-BR", "vi", "pl"):
            self.assertTrue(latin_language(language))

    def test_script_groups(self) -> None:
        self.assertEqual(script(HAN), "cjk")
        self.assertEqual(script("あ"), "cjk")
        self.assertEqual(script("한"), "cjk")
        self.assertEqual(script(CYRILLIC_A), "cyrillic")
        self.assertEqual(script(ARABIC), "arabic")
        for char in ("a", "é", GREEK_MU, "«", "、", "1", " "):
            self.assertIsNone(script(char), char)


class StreamTests(unittest.TestCase):
    def test_any_split_of_the_stream_gives_the_stored_answer(self) -> None:
        text = (
            f"Etude{HAN} : la piste ({KANJI_CITY}) est scientifiquement{HAN} active,\n"
            f"`code{HAN}x` et V{CYRILLIC_A}lmora{ARABIC} restent. Fin{HAN}"
        )
        stored, counts = clean(text, "fr")
        self.assertEqual(
            stored,
            f"Etude : la piste ({KANJI_CITY}) est scientifiquement active,\n"
            f"`code{HAN}x` et Valmora restent. Fin",
        )
        self.assertEqual(
            counts, {"removed": 4, "replaced": 1, "cjk": 3, "arabic": 1, "cyrillic": 1}
        )
        for size in (1, 2, 3, 7):
            with self.subTest(size=size):
                rewriter = Rewriter("fr")
                streamed = (
                    "".join(
                        rewriter.feed(text[start : start + size])
                        for start in range(0, len(text), size)
                    )
                    + rewriter.finish()
                )
                self.assertEqual(streamed, stored)

    def test_the_gate_holds_only_a_pending_run(self) -> None:
        sent: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            sent.append(event)

        async def play() -> None:
            gate = Gate(sink, Rewriter("fr"))
            await gate({"delta": {"content": "scientifiquement"}})
            await gate({"delta": {"content": HAN}})
            await gate({"delta": {"content": " active"}})
            await gate({"phase": "tool_started"})
            await gate.flush()

        asyncio.run(play())
        self.assertEqual(
            sent,
            [
                {"delta": {"content": "scientifiquement"}},
                {"delta": {"content": " active"}},
                {"phase": "tool_started"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
