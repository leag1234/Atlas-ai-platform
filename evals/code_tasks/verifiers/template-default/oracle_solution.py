def render(text, values, defaults):
    merged = dict(defaults)
    merged.update(values)
    return text.format(**merged)
