def parse_query(text):
    return {
        key: value
        for key, value in (
            part.split("=", 1) for part in text.split("&") if "=" in part
        )
    }
