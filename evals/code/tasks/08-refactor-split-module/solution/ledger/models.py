from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


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
