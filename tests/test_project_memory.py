"""M9 acceptance uses synthetic facts only; never reads interaction journals."""

import tempfile
import unittest
from pathlib import Path

from services.retrieval.projects import Projects


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "projects.sqlite"
        self.store = Projects(self.path)
        self.first = self.store.create("Premier", "")
        self.second = self.store.create("Second", "")
        self.chat = self.store.conversation(self.first, "Conversation A")

    def remember(self, text: str = "Le projet utilise le langage Python.") -> str:
        self.store.turn(self.first, self.chat, text, "Compris.", [text], 0)
        return str(self.store.facts(self.first)[0]["id"])

    def test_cross_conversation_and_persistence(self) -> None:
        self.remember()
        other = self.store.conversation(self.first, "Conversation B")
        reopened = Projects(self.path)
        context = reopened.context(
            self.first, other, "Quel langage utilise le projet ?"
        )
        self.assertIn("Python", str(context["facts"]))
        self.assertEqual(context["history"], [])

    def test_foreign_conversation_rejected(self) -> None:
        self.remember()
        with self.assertRaises(KeyError):
            self.store.context(self.second, self.chat, "langage")
        with self.assertRaises(KeyError):
            self.store.turn(self.second, self.chat, "question", "answer", [], 0)
        own = self.store.conversation(self.second, "Conversation C")
        self.assertEqual(self.store.context(self.second, own, "langage")["facts"], [])

    def test_erasure_invalidates_inflight_extraction(self) -> None:
        self.remember()
        self.store.erase(self.first)
        self.store.turn(self.first, self.chat, "Suite", "Compris", ["Python"], 0)
        self.assertEqual(self.store.facts(self.first), [])
        context = self.store.context(self.first, self.chat, "langage")
        self.assertNotIn("Python", str(context))

    def test_edit_invalidates_inflight_extraction(self) -> None:
        fact = self.remember()
        self.store.edit(self.first, fact, "Le projet utilise Rust.")
        self.store.turn(self.first, self.chat, "Suite", "Compris", ["Python"], 0)
        facts = self.store.facts(self.first)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["text"], "Le projet utilise Rust.")
        with self.assertRaises(KeyError):
            self.store.edit(self.second, fact, "Autre")
        with self.assertRaises(KeyError):
            self.store.erase(self.second, fact)

    def test_invalid_turn_is_atomic(self) -> None:
        with self.assertRaises(ValueError):
            self.store.turn(self.first, self.chat, "question", "answer", ["x" * 501], 0)
        self.assertEqual(
            self.store.context(self.first, self.chat, "question")["history"], []
        )
        self.assertEqual(self.store.facts(self.first), [])

    def test_private_file_permissions(self) -> None:
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_relevance_and_source_distinction(self) -> None:
        self.remember()
        other = self.store.conversation(self.first, "Conversation B")
        context = self.store.context(self.first, other, "langage")
        facts = context["facts"]
        assert isinstance(facts, list)
        self.assertEqual(facts[0]["kind"], "memory")
        self.assertEqual(self.store.context(self.first, other, "météo")["facts"], [])
