from decimal import ROUND_HALF_UP, Decimal


def quote(weight_kg, order_total, settings):
    weight = Decimal(str(weight_kg))
    total = Decimal(str(order_total))
    if weight <= 0:
        raise ValueError("weight must be positive")
    if settings.free_above is not None and total >= settings.free_above:
        return Decimal("0.00")
    price = settings.base_fee + settings.per_kg * weight
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
