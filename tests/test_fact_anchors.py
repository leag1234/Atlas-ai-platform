"""Specific claims of a sourced answer are anchored in the texts read (fictional content).

Defect class C3, invented facts: every year, full date, quarter, version, figure
with its unit or scale, place, organisation or product name and quoted title of
an answer written from sources must be found in the texts the turn read, or be
removed, sourced or marked as not verified. Code extracts and matches; the
model decides once what to do with what is unmatched (unsourced_details.py).
"""

import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator import grounding
from services.orchestrator import unsourced_details as details
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.unsourced_details import Evidence

SOURCES = {
    "fr": (
        "La coopérative Brelune a été fondée le 11 mars 2011 à "
        "Saint-Énoré-des-Combes. Elle compte 1 500 000 membres. Son rapport "
        "« Grands Livres verts » est paru au troisième trimestre 2024. Le logiciel "
        "de caisse Tarnweld 4.2.1 équipe ses 37 magasins. Le siège mesure 48 m."
    ),
    "en": (
        "Quillon Labs released Tarnweld version 4.2.1 on 3 May 2025. Revenue "
        "reached 12.4 million euros in Q3 2024. The firm, based in Vasselin, "
        "employs 47 120 people. Its Korvel K7 preamp offers 60 dB of gain."
    ),
    "de": (
        "Die Genossenschaft Brelune wurde am 11. März 2011 in Vasselin gegründet. "
        "Sie zählt 1 500 000 Mitglieder und erzielte im dritten Quartal 2024 einen "
        "Umsatz von 2,3 Mrd. Euro. Ihre Software Tarnweld 5.0 läuft in 37 Läden."
    ),
}

# Every specific claim is in the source, written another way.
ORACLES = {
    "fr": (
        "Brelune, coopérative fondée le 2011-03-11 à Saint-Enoré-des-Combes, compte "
        "environ 1,5 million de membres. Son rapport « Grands Livres verts » est "
        "sorti au T3 2024. Ses 37 magasins utilisent Tarnweld version 4.2.1, et "
        "son siège fait 48 mètres."
    ),
    "en": (
        "Quillon Labs shipped Tarnweld 4.2.1 on May 3, 2025. In the third quarter "
        "of 2024 its revenue was about 12 million euros. Based in Vasselin, it has "
        "roughly 47,000 employees, and the Korvel K7 preamp gives 60 dB of gain."
    ),
    "de": (
        "Die Genossenschaft Brelune entstand am 11.03.2011 in Vasselin. Heute hat "
        "die Genossenschaft rund 1,5 Mio. Mitglieder, die gemeinsam ihre Läden "
        "betreiben. Im Q3 2024 lag der Umsatz bei 2,3 Milliarden Euro. In den "
        "37 Läden läuft Tarnweld Version 5, gepflegt von einem Team aus "
        "Freiwilligen, das über die Preise und die Auswahl der Lieferanten entscheidet."
    ),
}

# One invented claim each, and the detail code must name.
INVENTED = [
    (
        "fr",
        "La coopérative Brelune a été fondée en 2020 à Saint-Énoré-des-Combes.",
        "2020",
    ),
    (
        "fr",
        "Brelune a publié son rapport (consulté le 14 février 2023).",
        "14 février 2023",
    ),
    (
        "fr",
        "Brelune a ouvert un magasin au village de Le Brix-sur-Orne.",
        "Brix-sur-Orne",
    ),
    (
        "fr",
        "Les magasins de Brelune utilisent Tarnweld version 4.3.",
        "Tarnweld version 4.3",
    ),
    (
        "en",
        "Quillon Labs released Tarnweld version 4.3.0 on 3 May 2025.",
        "Tarnweld version 4.3.0",
    ),
    ("en", "Quillon Labs released Tarnweld 4.2.7 on 3 May 2025.", "4.2.7"),
    ("en", "Revenue reached 12.4 million euros in Q2 2024.", "Q2 2024"),
    ("en", "The Korvel K7 preamp offers 71 dB of gain.", "71"),
    ("en", "The firm, based in Ostrelach, employs 47 120 people.", "Ostrelach"),
    (
        "de",
        "Die Genossenschaft Brelune wurde 2011 in Ostrelach gegründet.",
        "Ostrelach",
    ),
    ("de", "Brelune erzielte einen Umsatz von 2,8 Mrd. Euro.", "2,8 Mrd."),
    ("de", "Auf den Kassen läuft seit 2019 die Software Tarnweld 5.0.", "2019"),
]


def unmatched(answer: str, source: str) -> list[str]:
    found = details.unsupported(answer, Evidence.of([source]), Evidence.of([""]))
    return [detail.text for detail in found]


def gateway(text: str) -> AsyncMock:
    return AsyncMock(return_value={"text": text, "cost_eur": "0.0003"})


def sent(mock: AsyncMock) -> str:
    """The user message of the revision call."""
    assert mock.await_args is not None
    payload: dict[str, list[dict[str, str]]] = mock.await_args.args[0]
    return payload["messages"][1]["content"]


def review(
    answer: str, source: str, language: str, mock: AsyncMock, setting: str
) -> tuple[str, details.Report]:
    with patch.object(details, "_complete", mock):
        return asyncio.run(
            details.review(
                answer,
                [source],
                ["Parle-moi de Brelune"],
                profile="atlas-test",
                language=language,
                setting=setting,
                reference=False,
            )
        )


class AnchoredClaimsTests(unittest.TestCase):
    def test_oracle_answers_pass_in_three_languages(self) -> None:
        for language, answer in ORACLES.items():
            with self.subTest(language=language):
                self.assertEqual(unmatched(answer, SOURCES[language]), [])

    def test_oracle_answer_needs_no_model_call(self) -> None:
        mock = gateway("{}")
        answer, report = review(ORACLES["fr"], SOURCES["fr"], "fr", mock, "revise_mark")
        mock.assert_not_awaited()
        self.assertEqual(answer, ORACLES["fr"])
        self.assertEqual(report.unsupported, 0)
        self.assertEqual(report.metadata()["matched"], report.details)

    def test_invented_claims_are_caught(self) -> None:
        for language, answer, claim in INVENTED:
            with self.subTest(answer=answer):
                found = unmatched(answer, SOURCES[language])
                self.assertTrue(any(claim in text for text in found), found)

    def test_legitimate_variants_pass(self) -> None:
        pairs = [
            ("1 500 000 members", "1,5 million de membres"),
            ("1,5 million de membres", "1 500 000 membres"),
            ("founded on 11 mars 2011", "fondée le 2011-03-11"),
            ("fondée le 2011-03-11", "fondée le 11 mars 2011"),
            ("gegründet am 11. März 2011", "fondée le 11/03/2011"),
            ("revenue in Q3 2024", "au troisième trimestre 2024"),
            ("au troisième trimestre 2024", "im 3. Quartal 2024"),
            ("im dritten Quartal 2024", "in Q3 2024"),
            ("12.4 million euros", "about 12 million euros"),
            ("47 120 people", "about 47,000 people"),
            ("2,3 Mrd. Euro", "2.3 billion euros"),
            ("Tarnweld 5.0 is out", "Tarnweld version 5 is out"),
            ("Tarnweld 4 is out", "the Tarnweld version 4 is out"),
            ("at Saint-Énoré-des-Combes", "at Saint-Enore-des-Combes"),
            ("at Saint-Enore-des-Combes", "at Saint-Énoré-des-Combes"),
            ("a tower of 324 metres", "a tower of 324 m"),
        ]
        for source, answer in pairs:
            with self.subTest(answer=answer):
                self.assertEqual(unmatched("It was " + answer + ".", source), [])

    def test_variants_with_another_value_are_caught(self) -> None:
        pairs = [
            ("12.4 million euros", "about 13 million euros", "13 million"),
            ("in Q3 2024", "in the fourth quarter of 2024", "fourth quarter of 2024"),
            ("Tarnweld 5.0", "Tarnweld version 5.1", "Tarnweld version 5.1"),
            ("fondée le 11 mars 2011", "fondée le 12 mars 2011", "12 mars 2011"),
        ]
        for source, answer, claim in pairs:
            with self.subTest(answer=answer):
                self.assertIn(claim, unmatched("It was " + answer + ".", source))

    def test_german_nouns_are_not_names(self) -> None:
        answer = (
            "Die Mitglieder entscheiden über die Preise und die Auswahl der "
            "Lieferanten. Ein Vorstand aus Freiwilligen führt die Geschäfte in "
            "Zukunft mit regionalen Produkten."
        )
        self.assertEqual(unmatched(answer, SOURCES["de"]), [])
        # A place after "in" or "nach" stays a name to check.
        self.assertEqual(
            unmatched(
                "Die Mitglieder der Genossenschaft fahren jedes Jahr mit dem Bus "
                "nach Ostrelach und zur Messe.",
                SOURCES["de"],
            ),
            ["Ostrelach"],
        )

    def test_grouped_number_and_date_are_no_versions(self) -> None:
        kinds = {
            (d.kind, d.text)
            for d in details.extract(
                "Brelune zählt 1.234.567 Mitglieder seit dem 16.12.2004."
            )
        }
        self.assertNotIn("version", {kind for kind, _ in kinds})
        self.assertIn(("date", "16 12 2004"), kinds)

    def test_unsupported_claim_is_removed_and_counted(self) -> None:
        answer = (
            "La coopérative Brelune compte 1 500 000 membres.\n"
            "Elle a été fondée en 2020 à Saint-Énoré-des-Combes."
        )
        mock = gateway('{"1": "Elle a été fondée en 2011 à Saint-Énoré-des-Combes."}')
        final, report = review(answer, SOURCES["fr"], "fr", mock, "revise_mark")
        mock.assert_awaited_once()
        self.assertIn("fondée en 2011", final)
        metadata = report.metadata()
        self.assertEqual(metadata["unsupported"], 1)
        self.assertEqual(metadata["fixed"], 1)
        self.assertEqual(metadata["unsupported_after"], 0)
        self.assertEqual(metadata["kinds"], {"year": 1})
        self.assertEqual(metadata["marked"], 0)
        # Counts and codes only: the log never carries the answer's words.
        self.assertNotIn("2020", json.dumps(metadata))

    def test_model_may_keep_a_claim_marked_not_verified(self) -> None:
        answer = (
            "Quillon Labs employs 47 120 people.\n"
            "Its Korvel K7 preamp offers 71 dB of gain."
        )
        content = json.dumps(
            {"1": "Its Korvel K7 preamp offers 71 dB (not verified) of gain."}
        )
        mock = gateway(content)
        final, report = review(answer, SOURCES["en"], "en", mock, "revise_mark")
        self.assertIn("MARKER: (not verified)", sent(mock))
        self.assertIn("71 dB (not verified)", final)
        self.assertEqual(report.revised, "kept")
        self.assertEqual(report.model_marked, 1)
        self.assertEqual(report.unsupported_after, 0)
        # No second note from code on a claim the model already marked.
        self.assertEqual(report.marked, 0)
        self.assertNotIn("not found in the sources", final)

    def test_marker_follows_the_person_language(self) -> None:
        answer = "Brelune compte 1 500 000 membres.\nElle a été fondée en 2020."
        content = json.dumps({"1": "Elle a été fondée en 2020 (non vérifié)."})
        mock = gateway(content)
        final, report = review(answer, SOURCES["fr"], "fr", mock, "revise_mark")
        self.assertIn("MARKER: (non vérifié)", sent(mock))
        self.assertEqual(report.model_marked, 1)

    def test_marked_new_claim_is_rejected(self) -> None:
        answer = "Brelune compte 1 500 000 membres.\nElle a été fondée en 2020."
        content = json.dumps({"1": "Elle a été fondée en 2019 (non vérifié)."})
        final, report = review(answer, SOURCES["fr"], "fr", gateway(content), "revise")
        self.assertEqual(report.revised, "rejected:new_details")
        self.assertEqual(final, answer)

    def test_claim_still_unsupported_after_the_second_check_is_marked(self) -> None:
        answer = "Brelune compte 1 500 000 membres.\nElle a été fondée en 2020."
        final, report = review(
            answer, SOURCES["fr"], "fr", gateway("{}"), "revise_mark"
        )
        self.assertEqual(report.marked, 1)
        self.assertIn("(non trouvé dans les sources consultées : 2020)", final)

    def test_uncovered_answer_gets_one_closing_note(self) -> None:
        answer = (
            "Brelune a été fondée en 2019 par Ilse Varnholt à Ostrelach. Elle "
            "compte 812 salariés et 64 magasins depuis 2022.\n\n<details>\n"
            "<summary>Sources</summary>\n\n- <https://example.org/brelune>\n</details>"
        )
        mock = gateway("{}")
        final, report = review(answer, SOURCES["fr"], "fr", mock, "revise_mark")
        mock.assert_not_awaited()
        self.assertEqual(report.revised, "skipped:uncovered")
        self.assertEqual(report.marked, 1)
        self.assertIn("*(Non trouvé dans les sources consultées : ", final)
        self.assertTrue(final.endswith("</details>"))
        # Revise alone leaves such an answer as it was.
        final, report = review(answer, SOURCES["fr"], "fr", gateway("{}"), "revise")
        self.assertEqual(final, answer)

    def test_code_matching_stays_under_a_second(self) -> None:
        pages = [
            f"Brelune ouvre le magasin {index} à Vasselin-{index} le {1 + index % 28} "
            f"mars {1990 + index % 30}, avec {index * 37} m² et la version "
            f"{index % 9}.{index % 7}.{index % 5} de Tarnweld. "
            for index in range(600)
        ]
        answer = " ".join(
            f"Le magasin {index} de Vasselin-{index} a ouvert en {1990 + index % 30} "
            f"sur {index * 37} m² avec Tarnweld {index % 9}.{index % 7}.{index % 5}."
            for index in range(0, 600, 15)
        )
        started = time.monotonic()
        evidence = Evidence.of(pages)
        found = details.unsupported(answer, evidence, Evidence.of([""]))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual([d.text for d in found], [])


# A research answer streamed as written: one invented detail each, and the
# closing note in the person's language.
STREAMED = {
    "fr": (
        "Fais-moi un point sur la coopérative Brelune",
        "La coopérative Brelune a été fondée en 2020 à Saint-Énoré-des-Combes. "
        "Ses magasins utilisent Tarnweld version 4.2.7.",
        "*Non retrouvé dans les sources lues : 2020, Tarnweld version 4.2.7*",
    ),
    "en": (
        "Give me an overview of Quillon Labs",
        "Quillon Labs released Tarnweld 4.2.7 on 3 May 2025, and the firm is "
        "based in Vasselin.",
        "*Not found in the sources read: 4.2.7*",
    ),
    "de": (
        "Gib mir einen Überblick über die Genossenschaft Brelune",
        "Die Genossenschaft Brelune wurde 2011 in Ostrelach gegründet und zählt "
        "heute 1 500 000 Mitglieder.",
        "*In den gelesenen Quellen nicht gefunden: Ostrelach*",
    ),
}


class StreamedAnswerTests(unittest.IsolatedAsyncioTestCase):
    """A turn that streams gets the code check after it ends, and a note if needed."""

    async def turn(
        self,
        question: str,
        answer: str,
        source: str,
        *,
        ui: bool,
        setting: str = "revise_mark",
        held: bool = False,
    ) -> tuple[Interaction, list[str], AsyncMock]:
        async def generate(request: ChatRequest, item: Interaction) -> None:
            upstream = sink_context.get()
            assert upstream is not None
            # What the search tool returned, where the tool executor puts it.
            for collector in (grounding.current.get(), details.anchors.get()):
                if collector is not None:
                    collector.collect({"results": [{"snippet": source}]})
            await upstream({"delta": {"content": answer}})
            item.reponse = answer
            item.state = "done"

        sent: list[str] = []

        async def sink(event: dict[str, object]) -> None:
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                sent.append(delta["content"])

        mock = gateway("{}")
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": question}], "atlas_ui": ui}
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ATLAS_GROUNDING": "on", "ATLAS_UNSOURCED_DETAILS": setting},
                ),
                patch(
                    "services.orchestrator.chat_pipeline._process_request",
                    side_effect=generate,
                ),
                patch.object(grounding, "needs_grounding", return_value=held),
                patch.object(details, "wants_review", return_value=held),
                patch.object(details, "_complete", mock),
                patch.object(details, "references", AsyncMock(return_value=[])),
            ):
                await process(request, item)
        finally:
            sink_context.reset(token)
        return item, sent, mock

    async def test_ui_stream_gets_the_note_after_the_answer(self) -> None:
        for language, (question, answer, note) in STREAMED.items():
            with self.subTest(language=language):
                item, sent, mock = await self.turn(
                    question, answer, SOURCES[language], ui=True
                )
                mock.assert_not_awaited()
                self.assertEqual("".join(sent), answer + "\n\n" + note)
                self.assertEqual(item.reponse, answer + "\n\n" + note)
                steps = [s for s in item.steps if s.get("phase") == "anchor_check"]
                self.assertEqual(steps[0]["revised"], "noted")

    async def test_api_answers_carry_the_note_in_the_answer(self) -> None:
        # Stream or not, an API answer is sent whole after the checks
        # (chat_stream sends item.reponse): the note is part of it, never a
        # separate delta.
        for language, (question, answer, note) in STREAMED.items():
            with self.subTest(language=language):
                item, sent, mock = await self.turn(
                    question, answer, SOURCES[language], ui=False
                )
                mock.assert_not_awaited()
                self.assertNotIn(note, "".join(sent))
                self.assertEqual(item.reponse, answer + "\n\n" + note)

    async def test_oracle_answer_gets_no_note(self) -> None:
        for language, answer in ORACLES.items():
            for ui in (True, False):
                with self.subTest(language=language, ui=ui):
                    item, sent, _ = await self.turn(
                        "Parle-moi de Brelune", answer, SOURCES[language], ui=ui
                    )
                    self.assertEqual(item.reponse, answer)
                    self.assertNotIn("\n\n*", "".join(sent))

    async def test_held_turn_is_not_noted_twice(self) -> None:
        question, answer, note = STREAMED["fr"]
        item, sent, mock = await self.turn(
            question, answer, SOURCES["fr"], ui=True, held=True
        )
        mock.assert_awaited_once()
        self.assertNotIn("sources lues", item.reponse)
        self.assertFalse(any(s.get("phase") == "anchor_check" for s in item.steps))

    async def test_modes_without_marking_add_no_note(self) -> None:
        question, answer, _ = STREAMED["en"]
        for setting in ("off", "revise"):
            with self.subTest(setting=setting):
                item, sent, _ = await self.turn(
                    question, answer, SOURCES["en"], ui=True, setting=setting
                )
                self.assertEqual(item.reponse, answer)
                self.assertEqual("".join(sent), answer)

    async def test_turn_without_text_read_is_not_checked(self) -> None:
        question, answer, _ = STREAMED["en"]
        item, sent, _ = await self.turn(question, answer, "", ui=True)
        self.assertEqual(item.reponse, answer)
        self.assertFalse(any(s.get("phase") == "anchor_check" for s in item.steps))


class StreamedNoteTests(unittest.TestCase):
    def test_note_names_at_most_eight_details(self) -> None:
        answer = " ".join(
            f"Brelune opened shop {number} in {1900 + number}." for number in range(12)
        )
        note, report = details.streamed_check(
            answer, [SOURCES["en"]], [], language="en"
        )
        self.assertTrue(note.startswith("*Not found in the sources read: 1900"))
        self.assertTrue(note.endswith(", …*"))
        self.assertEqual(report.metadata()["marked"], 1)
        self.assertLess(report.match_seconds, 1.0)

    def test_note_goes_before_the_sources_block_when_not_streamed(self) -> None:
        answer = "Brelune text.\n\n<details>\n<summary>Sources</summary>\n</details>"
        stored = details.with_note(answer, "*note*", streamed=False)
        self.assertEqual(
            stored,
            "Brelune text.\n\n*note*\n\n<details>\n<summary>Sources</summary>\n</details>",
        )
        self.assertTrue(
            details.with_note(answer, "*note*", streamed=True).endswith("*note*")
        )


if __name__ == "__main__":
    unittest.main()
