import sys
from collections import defaultdict
from decimal import Decimal

from ledger.models import Account, LedgerError
from ledger.parse import parse_file


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
