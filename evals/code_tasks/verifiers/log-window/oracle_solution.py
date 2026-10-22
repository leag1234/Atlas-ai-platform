def recent(text, count):
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return lines[-count:]
