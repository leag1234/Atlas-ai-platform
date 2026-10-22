def build(base, parts, query):
    path = "/".join(part.strip("/") for part in parts if part.strip("/"))
    url = base.rstrip("/") + ("/" + path if path else "")
    pairs = [
        f"{key}={value}" for key, value in query.items() if value not in (None, "")
    ]
    return url + ("?" + "&".join(pairs) if pairs else "")
