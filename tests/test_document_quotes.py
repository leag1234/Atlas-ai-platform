"""Offline checks for word-for-word quotations of the user's documents (fictional data)."""

import json
import os
import time
import unittest
from unittest.mock import patch

from services.orchestrator import document_quotes
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.document_quotes import (
    NOTES,
    Rewriter,
    Sources,
    canonical,
    checked,
)
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context

CONTRACT_FR = (
    "CONTRAT DE PRESTATION (fictif)\n"
    "Article 5 – Sous-traitance\n"
    "Le Prestataire ne peut sous-traiter tout ou partie des prestations qu’avec "
    "l’accord préalable et écrit du Client. En cas de sous-traitance autorisée, le "
    "Prestataire demeure seul responsable envers le Client de la bonne exé-\n"
    "cution des prestations.\n"
    "Article 6 – Prix\n"
    "Le prix est fixé à 18 400 euros hors taxes, payable à trente jours fin de mois. "
    "Tout retard entraîne une pénalité égale à trois fois le taux d’intérêt légal."
)
REPORT_EN = (
    "Fennwick Valley Water Cooperative, annual report (fictional)\n"
    "In 2025, 23 percent of the water we treated was lost through leaks before it\n"
    "reached a meter, compared with 26 percent in 2024. The leak detection "
    "programme launched in March located 147 leaks, most of them on cast iron "
    "mains in the northern sector. The board will propose an increase of 6 percent "
    "of the volumetric price from 1 April 2026."
)
STATUTE_DE = (
    "Satzung des Vereins Gartenfreunde Lindenau (fiktiv)\n"
    "§ 4 Mitgliedschaft\n"
    "(1) Mitglied kann jede natürliche Person werden, die das sechzehnte Lebensjahr "
    "vollendet hat. Über den Aufnahmeantrag entscheidet der Vorstand.\n"
    "(2) Die Mitgliedschaft endet durch Austritt, Ausschluss oder Tod. Der Austritt "
    "ist nur zum Ende eines Kalenderjahres möglich."
)


def sources(*documents: str, said: str = "") -> Sources:
    found = Sources()
    for text in documents:
        found.add_document(text, "fictional-document.pdf")
    if said:
        found.add_conversation(said)
    return found


def run(
    answer: str,
    *documents: str,
    language: str = "en",
    asked: bool = False,
    said: str = "",
    evidence: str = "",
    chunk: int = 0,
) -> tuple[str, dict[str, int]]:
    held = sources(*documents, said=said)
    if evidence:
        held.collect({"url": "https://example.org", "content": evidence})
    rewriter = Rewriter(held, language=language, asked=asked)
    if chunk:
        text = "".join(
            rewriter.feed(answer[i : i + chunk]) for i in range(0, len(answer), chunk)
        )
    else:
        text = rewriter.feed(answer)
    return text + rewriter.finish(), rewriter.report()


class NormalizationTests(unittest.TestCase):
    def test_formatting_only_is_removed(self) -> None:
        self.assertEqual(
            canonical("L’acﬁon « exé-\n  cution » : 1 250 € — **OK**"),
            canonical("l'acfion exécution : 1250 € - ok"),
        )

    def test_wording_is_kept(self) -> None:
        self.assertNotEqual(
            canonical("payable à trente jours"), canonical("payable à 30 jours")
        )
        self.assertNotEqual(canonical("1,5 %"), canonical("15 %"))


class FrenchTests(unittest.TestCase):
    def test_exact_quotation_with_typographic_marks_and_line_break(self) -> None:
        answer = (
            "L’article 5 stipule : « le Prestataire demeure seul responsable envers le "
            "Client de la bonne exécution des prestations ». "
        )
        text, counts = run(answer, CONTRACT_FR, language="fr")
        self.assertEqual(text, answer)
        self.assertEqual(counts, {"exact": 1})

    def test_ellipsis_joins_verbatim_fragments(self) -> None:
        for gap in ("[…]", "(...)", "…", "[...]"):
            answer = (
                f"Le contrat prévoit : « Le Prestataire ne peut sous-traiter {gap} "
                "qu'avec l'accord préalable et écrit du Client »."
            )
            text, counts = run(answer, CONTRACT_FR, language="fr")
            self.assertEqual(text, answer)
            self.assertEqual(counts, {"ellipsis": 1})

    def test_one_word_changed_is_not_found_and_marked(self) -> None:
        answer = (
            "Selon l'article 6, « le prix est fixé à 18 400 euros hors taxes, payable "
            "à soixante jours fin de mois ». Il faut donc anticiper."
        )
        text, counts = run(answer, CONTRACT_FR, language="fr")
        self.assertEqual(counts, {"not_found": 1})
        self.assertNotIn("«", text)
        self.assertIn(
            "payable à soixante jours fin de mois *(" + NOTES["fr"] + ")*", text
        )
        self.assertTrue(text.startswith("Selon l'article 6, le prix est fixé"))
        self.assertTrue(text.endswith(". Il faut donc anticiper."))

    def test_fragments_out_of_order_are_not_found(self) -> None:
        answer = (
            "Le contrat dispose : « payable à trente jours fin de mois […] Le prix est "
            "fixé à 18 400 euros »."
        )
        _, counts = run(answer, CONTRACT_FR, language="fr")
        self.assertEqual(counts, {"not_found": 1})

    def test_invented_quotation_is_not_found(self) -> None:
        answer = "Le contrat précise : « le Client peut résilier à tout moment sans préavis »."
        text, counts = run(answer, CONTRACT_FR, language="fr")
        self.assertEqual(counts, {"not_found": 1})
        self.assertIn(NOTES["fr"], text)

    def test_suggested_wording_and_short_terms_are_not_checked(self) -> None:
        answer = (
            "Vous pourriez écrire au Client : « nous vous proposons un nouvel échéancier "
            "de paiement ». La notion de « sous-traitance autorisée » est centrale."
        )
        text, counts = run(answer, CONTRACT_FR, language="fr", asked=True)
        self.assertEqual(text, answer)
        self.assertEqual(counts, {"own_wording": 1, "short": 1})

    def test_external_law_quoted_from_memory_is_not_checked(self) -> None:
        answer = (
            "L'article 1103 du Code civil dispose : « les contrats légalement formés "
            "tiennent lieu de loi à ceux qui les ont faits »."
        )
        text, counts = run(answer, CONTRACT_FR, language="fr")
        self.assertEqual(text, answer)
        self.assertEqual(counts, {"external": 1})


class EnglishTests(unittest.TestCase):
    def test_exact_quotation_across_line_breaks_and_case(self) -> None:
        answer = (
            'The report states: "In 2025, 23 percent of the water we treated was lost '
            'through leaks before it reached a meter."'
        )
        text, counts = run(answer, REPORT_EN)
        self.assertEqual(text, answer)
        self.assertEqual(counts, {"exact": 1})

    def test_curly_quotes_and_changed_number(self) -> None:
        answer = (
            "According to the report, “the leak detection programme launched in March "
            "located 174 leaks”."
        )
        text, counts = run(answer, REPORT_EN)
        self.assertEqual(counts, {"not_found": 1})
        self.assertIn("located 174 leaks *(" + NOTES["en"] + ")*", text)

    def test_request_for_quotations_attributes_list_items(self) -> None:
        answer = (
            "Key findings:\n"
            '1. "The board will propose an increase of 6 percent of the volumetric '
            'price" (page 1)\n'
            '2. "Most leaks were found on plastic pipes in the southern sector"\n'
        )
        text, counts = run(answer, REPORT_EN, asked=True)
        self.assertEqual(counts, {"exact": 1, "not_found": 1})
        self.assertIn('1. "The board will propose', text)
        self.assertIn("2. Most leaks were found", text)

    def test_unattributed_quotation_without_request_is_left(self) -> None:
        answer = 'As the saying goes, "a stitch in time saves nine", so act early.'
        text, counts = run(answer, REPORT_EN)
        self.assertEqual(text, answer)
        self.assertEqual(counts, {"unattributed": 1})

    def test_user_words_and_web_pages_are_not_flagged(self) -> None:
        said = "My manager wrote: we must cut the leak rate by half before 2027."
        answer = (
            'You quoted your manager: "we must cut the leak rate by half before 2027". '
            'The utility\'s page reads: "smart meters cut losses by a fifth in two years".'
        )
        text, counts = run(
            answer,
            REPORT_EN,
            said=said,
            evidence="Field trials: smart meters cut losses by a fifth in two years.",
        )
        self.assertEqual(text, answer)
        self.assertEqual(counts, {"exact": 1, "elsewhere": 1})

    def test_block_quotation(self) -> None:
        exact = (
            "The report says:\n\n> The leak detection programme launched in March\n"
            "> located 147 leaks.\n\nThat is a lot."
        )
        text, counts = run(exact, REPORT_EN)
        self.assertEqual(text, exact)
        self.assertEqual(counts, {"exact": 1})
        altered = (
            "The report says:\n\n> The leak detection programme launched in May located"
            " 147 leaks.\n\nThat is a lot."
        )
        text, counts = run(altered, REPORT_EN)
        self.assertEqual(counts, {"not_found": 1})
        self.assertNotIn(">", text)
        self.assertIn(
            "launched in May located 147 leaks.\n\n*(" + NOTES["en"] + ")*", text
        )
        self.assertTrue(text.endswith("\n\nThat is a lot."))

    def test_block_paragraphs_are_checked_one_by_one(self) -> None:
        answer = (
            "Here is the passage of the report:\n\n"
            "> **Leaks**\n>\n"
            "> The leak detection programme launched in March located 147 leaks.\n>\n"
            "> **Tariff (page 1):** The board will propose an increase of 8 percent.\n"
            "\nDone."
        )
        text, counts = run(answer, REPORT_EN)
        self.assertEqual(counts, {"exact": 1, "not_found": 1})
        self.assertIn(
            "> The leak detection programme launched in March located 147 leaks.\n\n"
            "**Tariff (page 1):** The board will propose an increase of 8 percent."
            "\n\n*(" + NOTES["en"] + ")*\n\nDone.",
            text,
        )
        self.assertTrue(
            text.startswith("Here is the passage of the report:\n\n> **Leaks**")
        )

    def test_callout_block_is_not_a_quotation(self) -> None:
        answer = (
            "The report says a lot.\n\n> **Note:** check the leak figures with your "
            "own meter readings first.\n"
        )
        text, counts = run(answer, REPORT_EN, asked=True)
        self.assertEqual((text, counts), (answer, {"callout": 1}))

    def test_code_is_never_checked(self) -> None:
        answer = (
            'The report states it in `"leaks located in the north"` and:\n'
            '```\nprint("the board will propose an increase now")\n```\n'
        )
        text, counts = run(answer, REPORT_EN)
        self.assertEqual(text, answer)
        self.assertEqual(counts, {})

    def test_streamed_chunks_give_the_same_answer(self) -> None:
        answer = (
            "The report says “the leak detection programme launched in March located "
            "147 leaks” but also “the board rejected every increase”.\n\n"
            "> In 2025, 23 percent of the water was lost.\n\nEnd."
        )
        whole, counts = run(answer, REPORT_EN)
        for size in (1, 2, 5, 17):
            self.assertEqual(run(answer, REPORT_EN, chunk=size), (whole, counts))
        self.assertEqual(counts, {"exact": 1, "not_found": 2})


class PdfExtractionTests(unittest.TestCase):
    """Page furniture and split words of PDF extraction are formatting only."""

    PAGES = (
        "[Page 1]\nThe cooperative will raise the volumetric price by six percent and "
        "keep the fixed\nsubscription unchanged so that small consumers are 4\n"
        "protected from the increase.\n"
        "4 Fictional footnote: board minutes of 3 March, item 12.\n\n"
        "[Page 2]\n2\nFennwick Valley Water Cooperative\n"
        "while the reserve covers the de ficit of the year.\n"
    )

    def test_sentence_over_a_page_break_with_footnote_and_header(self) -> None:
        answer = (
            "The report states: “keep the fixed subscription unchanged so that small "
            "consumers are protected from the increase, while the reserve covers"
            "”."
        )
        _, counts = run(answer, self.PAGES)
        self.assertEqual(counts, {"exact": 1})

    def test_footnote_call_and_split_word(self) -> None:
        answer = (
            "The report states: “so that small consumers are protected from the "
            "increase”."
        )
        self.assertEqual(run(answer, self.PAGES)[1], {"exact": 1})
        answer = "The report states: “the reserve covers the deficit of the year”."
        self.assertEqual(run(answer, self.PAGES)[1], {"exact": 1})

    def test_a_changed_number_is_still_found_wrong(self) -> None:
        answer = (
            "The report states: “so that small consumers are 5 protected from the "
            "increase”."
        )
        self.assertEqual(run(answer, self.PAGES)[1], {"not_found": 1})

    def test_dropped_number_inside_a_sentence_is_not_found(self) -> None:
        document = "The 27 Member States agreed on the plan of the year.30\n"
        exact = "The report states: “the plan of the year”."
        self.assertEqual(run(exact, document)[1], {"exact": 1})
        dropped = "The report states: “The Member States agreed on the plan”."
        self.assertEqual(run(dropped, document)[1], {"not_found": 1})

    def test_footnote_line_and_split_word_together(self) -> None:
        document = (
            "Only a third of the patents filed by the univer sities of the Union\n4\n"
            "are com\nmercially exploited.\n"
        )
        answer = (
            "The report states: “Only a third of the patents filed by the "
            "universities of the Union are commercially exploited.”"
        )
        self.assertEqual(run(answer, document)[1], {"exact": 1})

    def test_words_skipped_far_from_a_page_break_are_not_found(self) -> None:
        answer = (
            "The report states: “The cooperative will raise the volumetric price "
            "while the reserve covers the deficit of the year”."
        )
        self.assertEqual(run(answer, self.PAGES)[1], {"not_found": 1})


class GermanTests(unittest.TestCase):
    def test_low_quotes_exact_and_altered(self) -> None:
        exact = (
            "§ 4 Absatz 2 lautet: „Die Mitgliedschaft endet durch Austritt, Ausschluss "
            "oder Tod.“"
        )
        text, counts = run(exact, STATUTE_DE, language="de")
        self.assertEqual((text, counts), (exact, {"exact": 1}))
        altered = (
            "Laut der Satzung gilt: „Mitglied kann jede natürliche Person werden, die "
            "das achtzehnte Lebensjahr vollendet hat.“"
        )
        text, counts = run(altered, STATUTE_DE, language="de")
        self.assertEqual(counts, {"not_found": 1})
        self.assertIn(NOTES["de"], text)
        self.assertNotIn("„", text)

    def test_single_quotes_and_apostrophes(self) -> None:
        answer = (
            "Im Dokument steht ‚Über den Aufnahmeantrag entscheidet der Vorstand‘ – "
            "so ist's geregelt."
        )
        text, counts = run(answer, STATUTE_DE, language="de")
        self.assertEqual((text, counts), (answer, {"exact": 1}))


class ScopeTests(unittest.TestCase):
    def test_no_document_no_check(self) -> None:
        answer = 'The article states: "nothing here is from any document at all".'
        held = Sources()
        held.add_conversation("Short question?")
        text, counts = checked(held, answer, "en", "Short question?")
        self.assertEqual((text, counts), (answer, {}))

    def test_pasted_text_is_a_document(self) -> None:
        pasted = "Please check this text: " + REPORT_EN * 2
        held = Sources()
        held.add_conversation(pasted)
        answer = 'Your text states: "the board will propose an increase of 9 percent".'
        with patch.dict(os.environ, {"ATLAS_DOCUMENT_QUOTES": "mark"}):
            text, counts = checked(held, answer, "en", pasted)
        self.assertEqual(counts, {"not_found": 1})
        self.assertIn(NOTES["en"], text)

    def test_log_mode_counts_without_changing(self) -> None:
        answer = "The report states: “the board rejected every tariff increase”."
        with patch.dict(os.environ, {"ATLAS_DOCUMENT_QUOTES": "log"}):
            text, counts = checked(sources(REPORT_EN), answer, "en", "Quote it")
        self.assertEqual((text, counts), (answer, {"not_found": 1}))


class ScanCostTests(unittest.TestCase):
    """Hostile shapes stay linear: one pass, bounded holds, no rescan."""

    def assert_fast(self, answer: str, *documents: str) -> None:
        started = time.monotonic()
        held = sources(*documents)
        rewriter = Rewriter(held, asked=True)
        text = "".join(
            rewriter.feed(answer[i : i + 64]) for i in range(0, len(answer), 64)
        )
        text += rewriter.finish()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertGreater(len(text), len(answer) // 2)

    def test_unclosed_openings(self) -> None:
        self.assert_fast("« le contrat stipule " * 20_000, CONTRACT_FR)

    def test_many_quotations_and_blocks(self) -> None:
        self.assert_fast(
            (
                'The report says "the board will propose an increase now". \n> quote line\n'
                * 5_000
            ),
            REPORT_EN * 200,
        )

    def test_long_document_and_quotation(self) -> None:
        document = ("word " * 400_000) + "the final sentence of the long document."
        self.assert_fast(
            'The document states: "' + "word " * 300 + 'the final sentence"', document
        )


class PageScanCostTests(unittest.TestCase):
    def test_many_pages_and_quotations_not_found(self) -> None:
        pages = "".join(
            f"[Page {index}]\n" + "alpha beta gamma delta " * 100 + "\n"
            for index in range(400)
        )
        quotation = (
            'The document states: "' + "alpha beta gamma delta " * 50 + 'omega" '
        )
        held = sources(pages)
        started = time.monotonic()
        rewriter = Rewriter(held, asked=True)
        rewriter.feed(quotation * 80)
        rewriter.finish()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(rewriter.report(), {"not_found": 60})


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, mode: str) -> tuple[Interaction, list[str], list[str]]:
        answer = (
            "Le contrat stipule : « Tout retard entraîne une pénalité égale à deux fois "
            "le taux d’intérêt légal »."
        )
        streamed: list[str] = []

        async def sink(event: dict[str, object]) -> None:
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                streamed.append(str(delta["content"]))

        async def generate(request: ChatRequest, item: Interaction) -> None:
            document_quotes.add_document(CONTRACT_FR, "contrat-fictif.docx")
            stream = sink_context.get()
            assert stream is not None
            for start in range(0, len(answer), 7):
                await stream({"delta": {"content": answer[start : start + 7]}})
            await stream({"phase": "document_read"})
            item.reponse = answer
            item.state = "done"

        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Que dit le contrat sur les retards ?"}
                ]
            }
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with (
                patch.dict(os.environ, {"ATLAS_DOCUMENT_QUOTES": mode}),
                patch(
                    "services.orchestrator.chat_pipeline._process_request",
                    side_effect=generate,
                ),
                self.assertLogs(document_quotes.LOGGER, "INFO") as logs,
            ):
                document_quotes.LOGGER.info("start")
                await process(request, item)
        finally:
            sink_context.reset(token)
        return (
            item,
            streamed,
            [line for line in logs.output if "document_quotes " in line],
        )

    async def test_mark_mode_streams_and_stores_the_same_marked_answer(self) -> None:
        item, streamed, lines = await self.run_turn("mark")
        self.assertEqual("".join(streamed), item.reponse)
        self.assertNotIn("«", item.reponse)
        self.assertIn(NOTES["fr"], item.reponse)
        self.assertEqual(len(lines), 1)
        fields = json.loads(lines[0].split("document_quotes ", 1)[1])
        self.assertEqual(fields["not_found"], 1)
        self.assertEqual(fields["documents"], 1)
        # Counts only: no document or answer text reaches the log.
        self.assertNotIn("retard", lines[0])
        self.assertNotIn("contrat", lines[0])

    async def test_off_leaves_everything(self) -> None:
        item, streamed, lines = await self.run_turn("off")
        self.assertIn("«", item.reponse)
        self.assertIn("«", "".join(streamed))
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
