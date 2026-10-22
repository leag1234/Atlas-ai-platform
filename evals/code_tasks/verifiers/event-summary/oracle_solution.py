def summarize(events):
    result = {}
    for event in events:
        kind = event["kind"]
        result[kind] = result.get(kind, 0) + 1
    return result
