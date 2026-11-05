from decimal import ROUND_HALF_UP, Decimal

from shipping.config import CONFIG


def quote(weight_kg, order_total):
    weight = Decimal(str(weight_kg))
    total = Decimal(str(order_total))
    if weight <= 0:
        raise ValueError("weight must be positive")
    free_above = CONFIG["free_above"]
    if free_above is not None and total >= free_above:
        return Decimal("0.00")
    price = CONFIG["base_fee"] + CONFIG["per_kg"] * weight
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
