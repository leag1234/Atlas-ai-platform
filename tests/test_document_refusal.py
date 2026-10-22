"""Encrypted-document failures require both the cause and an explicit inability."""

import copy
import gzip
import json
from pathlib import Path
import unittest

from m24_artifacts import validate


class DocumentRefusalTests(unittest.TestCase):
    def test_recorded_explicit_inability_with_connective(self) -> None:
        case = json.loads(
            gzip.decompress(Path("tests/cassettes/m24.json.gz").read_bytes())
        )["J46"]
        validate("J46", case, case["response"])

    def test_encryption_mention_or_unexplained_refusal_is_insufficient(self) -> None:
        saved = json.loads(
            gzip.decompress(Path("tests/cassettes/m24.json.gz").read_bytes())
        )["J46"]
        for text in (
            "Le PDF est chiffré. Voici son résumé : tout est approuvé.",
            "Je ne peux donc pas résumer ce document.",
        ):
            with self.subTest(text=text):
                case = copy.deepcopy(saved)
                case["response"]["choices"][0]["message"]["content"] = text
                with self.assertRaises(AssertionError):
                    validate("J46", case, case["response"])
