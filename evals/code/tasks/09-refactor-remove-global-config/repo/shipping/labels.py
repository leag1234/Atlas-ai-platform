from shipping import config


def make_label(order_id):
    return f"{config.CONFIG['label_prefix']}-{int(order_id):08d}"
