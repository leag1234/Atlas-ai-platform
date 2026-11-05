import dataclasses
import json
from dataclasses import dataclass
from decimal import Decimal

AMOUNTS = ("base_fee", "per_kg", "free_above")


@dataclass(frozen=True)
class Settings:
    currency: str = "EUR"
    base_fee: Decimal = Decimal("4.90")
    per_kg: Decimal = Decimal("1.20")
    free_above: Decimal | None = Decimal("60.00")
    label_prefix: str = "SHP"


def load_settings(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    known = {f.name for f in dataclasses.fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    values = {k: (Decimal(v) if k in AMOUNTS and v is not None else v) for k, v in data.items()}
    return Settings(**values)
