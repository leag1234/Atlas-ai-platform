"""Numeric validation errors disclose bounds without reflecting private inputs."""

import unittest
from pydantic import BaseModel, Field, ValidationError
from packages.validation import describe_validation


class Payload(BaseModel):
    text: str = Field(max_length=4)
    count: int = Field(le=8)


class ValidationDiagnosticsTests(unittest.TestCase):
    def test_bounds_without_contents(self) -> None:
        try:
            Payload(text="secret contents", count=9)
        except ValidationError as exc:
            detail = describe_validation(exc)
        self.assertIn("measured length 15", detail)
        self.assertIn("max_length 4", detail)
        self.assertIn("measured value 9", detail)
        self.assertIn("le 8", detail)
        self.assertNotIn("secret", detail)
