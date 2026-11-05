import json
from decimal import Decimal

CONFIG = {
    "currency": "EUR",
    "base_fee": Decimal("4.90"),
    "per_kg": Decimal("1.20"),
    "free_above": Decimal("60.00"),
    "label_prefix": "SHP",
}


def load(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    for key, value in data.items():
        CONFIG[key] = Decimal(value) if key in ("base_fee", "per_kg", "free_above") and value is not None else value
