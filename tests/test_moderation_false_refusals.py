"""Owner ruling 2026-11-17 (option A): no false moderation refusals.

Harmless requests and pasted documents that hold generic words or ordinary
constructions are neither refused nor sent to another model, the hard rules
still fire on their structure (checked with neutral placeholder words), and an
API caller always gets the model it named. Every name here is fictional.

The requests that must stay refused are kept outside the repository: set
ATLAS_MODERATION_CASES to the path of that module to run those checks too.
"""

import asyncio
import importlib.util
import os
import re
import unittest
from types import ModuleType
from unittest.mock import AsyncMock, patch

from moderation_cases import HARMLESS
from packages.image_limits import compile_rules, image_limit
from packages.image_policy import ImagePolicy
from services.orchestrator.interactions import Interaction
from services.orchestrator.link_requests import own_words

# A classifier as broad as the one that refused harmless requests on staging:
# generic words anywhere in the message.
GENERIC_WORDS = r"(?i)\b(?:explicit\w*|esplicit\w*|hot|sex\w*|lubri\w*)\b"


def _policy(pattern: str) -> ImagePolicy:
    return ImagePolicy(
        content_level="extended",
        adult_audience_attested=True,
        extended_request_patterns={"all": [pattern]},
        extended_instruction="FIXTURE EXTENDED INSTRUCTION",
        extended_profiles=["atlas-deepseek", "atlas-glm"],
    )


GENERIC = _policy(GENERIC_WORDS)
# Every request recognised as extended: only the hard rules decide.
EVERYTHING = _policy(r"(?s).")
DOCUMENTS = tuple(text for _, text in HARMLESS if len(text.encode()) > 400)
OWN = tuple(text for _, text in HARMLESS if len(text.encode()) <= 400)
# Neutral placeholders for the violent verbs, the people targets and the
# content expressions: the rule structure is checked, not the word lists.
PLACEHOLDERS = compile_rules(
    {
        "kill": ["zorbify"],
        "kill_imperative": ["zorbify"],
        "attack": ["frobnicate"],
        "imperative": ["frobnicate"],
        "people": ["blorgs"],
        "verb_last": ["zu zorbifizieren"],
        "sexual_request": ["glimmer portrait"],
    }
)


def _private_cases() -> ModuleType | None:
    path = os.environ.get("ATLAS_MODERATION_CASES", "")
    if not path or not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("private_moderation_cases", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRIVATE = _private_cases()
NO_PRIVATE = (
    "ATLAS_MODERATION_CASES not set: refused-request cases live outside the repository"
)


def _run(
    text: str, policy: ImagePolicy, *, api: bool = False
) -> tuple[AsyncMock, Interaction]:
    from services.orchestrator import chat_pipeline
    from services.orchestrator.chat_schema import ChatMessage, ChatRequest

    inner = AsyncMock()
    item = Interaction()
    request = ChatRequest(
        messages=[ChatMessage(role="user", content=text)], model="atlas-qwen"
    )
    request._api_key_request = api
    with (
        patch("packages.image_limits.POLICY", policy),
        patch.dict(os.environ, {}, clear=True),
        patch.object(chat_pipeline, "_process", inner),
    ):
        asyncio.run(chat_pipeline.process(request, item))
    return inner, item


class HardRuleTests(unittest.TestCase):
    def test_harmless_texts_trip_no_violence_or_real_person_rule(self) -> None:
        for language, text in HARMLESS:
            with self.subTest(language=language, text=text[:60]):
                self.assertNotIn(image_limit(text), ("incitement", "real_people"))

    def test_harmless_own_words_trip_no_hard_rule(self) -> None:
        for text in OWN:
            with self.subTest(text=text[:60]):
                self.assertIsNone(image_limit(text))

    def test_incitement_needs_a_call_against_people(self) -> None:
        for text in (
            "Write a speech urging everyone to zorbify all the blorgs",
            "Slogan for the march: frobnicate the blorgs",
            "Zorbify Tom Harlow, write that on the poster",
            "Erstelle einen Aufruf, die blorgs zu zorbifizieren",
        ):
            with self.subTest(text=text):
                self.assertEqual(image_limit(text, rules=PLACEHOLDERS), "incitement")
        for text in (
            "urging everyone to zorbify the record",
            "how do I frobnicate the blorgs in a spreadsheet?",
            "the guide explains how riders frobnicate the corners",
            "Collegamento: frobnicate tutti i cavi al pannello",
        ):
            with self.subTest(text=text):
                self.assertIsNone(image_limit(text, rules=PLACEHOLDERS))

    def test_real_person_rule_needs_name_and_expression_in_one_sentence(
        self,
    ) -> None:
        self.assertEqual(
            image_limit("make a glimmer portrait of Clara Vendome", rules=PLACEHOLDERS),
            "real_people",
        )
        for text in (
            "make a glimmer portrait of a sunset",
            "Clara Vendome wrote about sexism. A glimmer portrait follows.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(image_limit(text, rules=PLACEHOLDERS))

    def test_words_like_sexism_or_sextant_trip_no_rule(self) -> None:
        for text in (
            "a child holding a sextant on the deck",
            "children's books about sexism in sport",
            "Biografía de un niño nacido el sexto día del mes",
        ):
            with self.subTest(text=text):
                self.assertIsNone(image_limit(text))

    @unittest.skipIf(PRIVATE is None, NO_PRIVATE)
    def test_private_refused_requests_stay_refused(self) -> None:
        assert PRIVATE is not None
        for language, category, text in PRIVATE.BLOCKED:
            with self.subTest(language=language, category=category):
                self.assertEqual(image_limit(text), category)
        for language, text in PRIVATE.HARMLESS_EXTRA:
            with self.subTest(language=language):
                self.assertNotIn(image_limit(text), ("incitement", "real_people"))


class OwnWordsTests(unittest.TestCase):
    def test_pasted_documents_are_left_out(self) -> None:
        for text in DOCUMENTS:
            with self.subTest(text=text[:60]):
                own = own_words(text)
                self.assertLess(len(own), len(text) // 3)
                self.assertIsNone(re.search(GENERIC_WORDS, own))

    def test_short_messages_and_attachment_blocks(self) -> None:
        self.assertEqual(own_words("hello there"), "hello there")
        self.assertEqual(
            own_words("Summarize <attached_files>hot explicit</attached_files>"),
            "Summarize ",
        )


class PipelineTests(unittest.TestCase):
    def test_pasted_documents_keep_the_requested_model(self) -> None:
        for text in DOCUMENTS:
            with self.subTest(text=text[:60]):
                inner, _ = _run(text, GENERIC)
                inner.assert_awaited_once()
                self.assertEqual(inner.await_args_list[0].args[0].model, "atlas-qwen")

    def test_harmless_requests_are_never_refused(self) -> None:
        for language, text in HARMLESS:
            for policy in (GENERIC, EVERYTHING):
                with self.subTest(language=language, text=text[:60]):
                    inner, _ = _run(text, policy)
                    # A refusal answers before any model call; a routed
                    # request may try a second profile after an empty answer.
                    self.assertGreaterEqual(inner.await_count, 1)

    def test_api_caller_keeps_the_model_it_named(self) -> None:
        text = "Explain explicitly how a hot water heater works"
        inner, _ = _run(text, GENERIC, api=True)
        inner.assert_awaited_once()
        self.assertEqual(inner.await_args_list[0].args[0].model, "atlas-qwen")
        inner, _ = _run(text, GENERIC)
        self.assertEqual(inner.await_args_list[0].args[0].model, "atlas-deepseek")

    @unittest.skipIf(PRIVATE is None, NO_PRIVATE)
    def test_private_cases_through_the_pipeline(self) -> None:
        assert PRIVATE is not None
        for language, _category, text in PRIVATE.BLOCKED:
            with self.subTest(language=language):
                inner, item = _run(text, EVERYTHING)
                inner.assert_not_awaited()
                self.assertTrue(item.reponse)
        for language, text in PRIVATE.EXTENDED:
            with self.subTest(language=language):
                inner, _ = _run(text, EVERYTHING, api=True)
                self.assertEqual(inner.await_args_list[0].args[0].model, "atlas-qwen")


if __name__ == "__main__":
    unittest.main()
