"""Plain-text double-entry ledger: model, parser and reports in one file."""

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


class LedgerError(ValueError):
    """Raised for malformed ledger input."""


@dataclass(frozen=True)
class Entry:
    day: date
    debit: str
    credit: str
    amount: Decimal
    memo: str = ""


@dataclass
class Account:
    name: str
    balance: Decimal = Decimal("0.00")
    entries: list = field(default_factory=list)

    @property
    def kind(self):
        return self.name.split(":", 1)[0]

    def apply(self, entry):
        if entry.debit == self.name:
            self.balance += entry.amount
        elif entry.credit == self.name:
            self.balance -= entry.amount
        else:
            raise LedgerError(f"entry does not touch {self.name}")
        self.entries.append(entry)


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


def balances(entries):
    accounts = {}
    for entry in entries:
        for name in (entry.debit, entry.credit):
            accounts.setdefault(name, Account(name)).apply(entry)
    return dict(sorted(accounts.items()))


def monthly_totals(entries, kind="expenses"):
    totals = defaultdict(lambda: Decimal("0.00"))
    for entry in entries:
        if entry.debit.split(":", 1)[0] == kind:
            totals[entry.day.strftime("%Y-%m")] += entry.amount
    return dict(sorted(totals.items()))


def format_report(entries):
    lines = []
    for name, account in balances(entries).items():
        lines.append(f"{name:<20}{account.balance:>12}")
    lines.append("")
    for month, total in monthly_totals(entries).items():
        lines.append(f"expenses {month}{total:>16}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: ledger FILE", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(format_report(parse_file(argv[0])))
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
