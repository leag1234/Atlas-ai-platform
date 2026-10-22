def matches(value, includes, excludes):
    return any(value.startswith(prefix) for prefix in includes) and not any(
        value.startswith(prefix) for prefix in excludes
    )
