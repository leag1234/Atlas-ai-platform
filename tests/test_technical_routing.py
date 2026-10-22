"""Software throughput analysis belongs to code expertise, with both profiles shared."""

import unittest
from serverless import classify_task


class TechnicalRoutingTests(unittest.TestCase):
    def test_instruction_and_software_performance_phrasings(self) -> None:
        for question in (
            "Quel débit maximum peut générer un logiciel sur cette CPU ?",
            "CPU DEBIT LOGICIEL MAXIMUM",
            "How does processor speed limit software throughput?",
            "Quel débit pour ce programme assembleur ?",
            "Compare these assembly loops.",
            "Assembler: wie viele Takte braucht diese Schleife?",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    classify_task([{"role": "user", "content": question}]), "code"
                )

    def test_hardware_specs_are_text(self) -> None:
        for question in (
            "What is the CPU clock speed of this computer?",
            "Quelle interface série choisir pour ce logiciel ?",
            "Quel est le débit de cette connexion ?",
        ):
            self.assertEqual(
                classify_task([{"role": "user", "content": question}]), "text"
            )
