def slugs(titles):
    result, counts = [], {}
    for title in titles:
        base = title.lower().replace(" ", "-")
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base}-{counts[base]}")
    return result
