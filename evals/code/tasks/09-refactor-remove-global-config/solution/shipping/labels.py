def make_label(order_id, settings):
    return f"{settings.label_prefix}-{int(order_id):08d}"
