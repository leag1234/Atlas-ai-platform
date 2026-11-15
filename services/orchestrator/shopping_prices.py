"""Shopping amounts need evidence tied to the linked page or search snippet."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from packages import linear_scan
from services.orchestrator.source_truth import urls

MONEY = re.compile(
    r"(?:(?P<before>€|\$|£|EUR\b|USD\b|GBP\b)\s*(?P<left>\d[\d .,\u00a0]*\d|\d)"
    r"|(?P<right>\d[\d .,\u00a0]*\d|\d)\s*(?P<after>€|\$|£|EUR\b|USD\b|GBP\b))",
    re.I,
)
# An amount before its currency is read from the first digit of a run of digits,
# spaces and separators to the run's last digit, whatever digit it starts at: a
# failed attempt fails from every digit of its run, and retrying each one was
# quadratic on long digit runs (number tables in fetched pages).
_NUMBER_RUN = re.compile(r"[\d .,\u00a0]*+")


def _money_failure(text: str) -> linear_scan.FailedUntil:
    def failed_until(found: re.Match[str]) -> int:
        if not found[0].isdigit():
            return found.end()
        run = _NUMBER_RUN.match(text, found.start())
        return run.end() if run else found.end()

    return failed_until


MONEYS = linear_scan.Scan(
    MONEY, re.compile(r"€|\$|£|EUR\b|USD\b|GBP\b|\d", re.I), _money_failure
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
        url: {amount(m) for m in MONEYS.finditer(page)} for url, page in pages.items()
    }
    snippet_evidence = {
        url: {amount(m) for m in MONEYS.finditer(text)}
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

        lines.append(MONEYS.sub(replace, line))
    return "\n".join(lines)
