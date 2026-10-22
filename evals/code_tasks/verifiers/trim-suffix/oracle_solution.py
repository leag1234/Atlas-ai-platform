def trim_suffix(value, suffix):
    return value[: -len(suffix)] if suffix and value.endswith(suffix) else value
