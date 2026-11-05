from shipping.labels import make_label
from shipping.rates import quote


def render(order_id, weight_kg, order_total, settings):
    fee = quote(weight_kg, order_total, settings)
    return f"{make_label(order_id, settings)} shipping {fee} {settings.currency}"
