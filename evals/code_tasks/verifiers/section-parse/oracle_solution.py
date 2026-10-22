def parse(text):
    result, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            result.setdefault(section, {})
        elif "=" in line and section is not None:
            key, value = line.split("=", 1)
            result[section][key.strip()] = value.strip()
    return result
