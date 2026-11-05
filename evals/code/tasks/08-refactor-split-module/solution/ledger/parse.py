from datetime import date
from decimal import Decimal, InvalidOperation

from ledger.models import Entry, LedgerError


def parse_amount(text):
    cleaned = text.strip().replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise LedgerError(f"bad amount: {text!r}") from None
    if value <= 0:
        raise LedgerError(f"amount must be positive: {text!r}")
    return value.quantize(Decimal("0.01"))


def parse_line(line, lineno=0):
    parts = [p.strip() for p in line.split("|")]
    if len(parts) not in (4, 5):
        raise LedgerError(f"line {lineno}: expected 4 or 5 fields")
    try:
        day = date.fromisoformat(parts[0])
    except ValueError:
        raise LedgerError(f"line {lineno}: bad date") from None
    memo = parts[4] if len(parts) == 5 else ""
    return Entry(day, parts[1], parts[2], parse_amount(parts[3]), memo)


def parse_file(path):
    entries = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            entries.append(parse_line(line, lineno))
    return entries
