"""Deterministic structured-content rendering, independent of Atlas services."""

from .renderer import render
from .schema import Content, PresentationOutline, PresentationSlide, Section, Table
from .verify import Verification, VerificationError, verify

__all__ = [
    "Content",
    "PresentationOutline",
    "PresentationSlide",
    "Section",
    "Table",
    "Verification",
    "VerificationError",
    "render",
    "verify",
]
