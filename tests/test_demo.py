import unittest

from m6_demo import validate
from services.orchestrator.loop import Result


class DemoTests(unittest.TestCase):
    def test_missing_proof_never_completes_demo(self) -> None:
        for ident in ("DEMO-RAG", "DEMO-WEB", "DEMO-FALLBACK"):
            with self.assertRaises(ValueError):
                validate(ident, Result(text="unsupported", state="done"), False)
        with self.assertRaises(ValueError):
            validate("DEMO-FALLBACK", Result(state="budget_exceeded"), True)
        validate("DEMO-FALLBACK", Result(text="Bonjour", state="done"), True)
