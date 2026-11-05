import csv
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    quantity: int
    unit_price: Decimal

    @property
    def value(self):
        return (self.unit_price * self.quantity).quantize(CENT, rounding=ROUND_HALF_UP)


def load(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return [
            Item(row["sku"], row["name"], int(row["quantity"]), Decimal(row["unit_price"]))
            for row in csv.DictReader(handle)
        ]
