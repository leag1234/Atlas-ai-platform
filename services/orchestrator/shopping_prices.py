"""Shopping amounts need evidence tied to the linked page or search snippet."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from services.orchestrator.source_truth import urls

MONEY = re.compile(
    r"(?:(?P<before>€|\$|£|EUR\b|USD\b|GBP\b)\s*(?P<left>\d[\d .,\u00a0]*\d|\d)"
    r"|(?P<right>\d[\d .,\u00a0]*\d|\d)\s*(?P<after>€|\$|£|EUR\b|USD\b|GBP\b))",
    re.I,
)


def amount(match: re.Match[str]) -> tuple[str, Decimal] | None:
    number = re.sub(r"[\s\u00a0]", "", match["left"] or match["right"])
    number = re.sub(r"[.,](?=\d{3}(?:[.,]|$))", "", number).replace(",", ".")
    currency = (match["before"] or match["after"]).upper()
    currency = {"€": "EUR", "$": "USD", "£": "GBP"}.get(currency, currency)
    try:
        return currency, Decimal(number)
    except InvalidOperation:
        return None


def verified_prices(
    text: str,
    pages: dict[str, str],
    language: str,
    snippets: dict[str, str] | None = None,
) -> str:
    evidence = {
        url: {amount(m) for m in MONEY.finditer(page)} for url, page in pages.items()
    }
    snippet_evidence = {
        url: {amount(m) for m in MONEY.finditer(text)}
        for url, text in (snippets or {}).items()
    }
    labels = {
        "fr": ("prix non vérifié", "prix au moment de la lecture"),
        "de": ("Preis nicht verifiziert", "Preis zum Zeitpunkt des Abrufs"),
        "en": ("price not verified", "price at the time of reading"),
        "es": ("precio no verificado", "precio en el momento de la lectura"),
        "it": ("prezzo non verificato", "prezzo al momento della lettura"),
    }
    missing, timestamp = labels.get(language, labels["en"])
    lines = []
    for line in text.splitlines():
        # Requiring the link in the same bullet/table row avoids transferring a
        # price from a different product or shop. Ambiguous amounts are omitted.
        linked = urls(line)
        sources = {
            value: (url, "page")
            for url in sorted(linked)
            for value in evidence.get(url, set())
        }
        for url in sorted(linked):
            for value in snippet_evidence.get(url, set()):
                sources.setdefault(value, (url, "search snippet"))

        def replace(match: re.Match[str]) -> str:
            value = amount(match)
            if value is None or value not in sources:
                return missing
            url, kind = sources[value]
            qualifier = "" if timestamp in line else " (" + timestamp + ")"
            return (
                match[0]
                + qualifier
                + " [source: "
                + kind
                + " — "
                + urlsplit(url).netloc
                + "]"
            )

        lines.append(MONEY.sub(replace, line))
    return "\n".join(lines)
