from shipping.config import CONFIG
from shipping.labels import make_label
from shipping.rates import quote


def render(order_id, weight_kg, order_total):
    fee = quote(weight_kg, order_total)
    return f"{make_label(order_id)} shipping {fee} {CONFIG['currency']}"
