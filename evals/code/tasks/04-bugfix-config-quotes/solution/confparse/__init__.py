"""Parser for the configuration format in FORMAT.md."""


class ParseError(ValueError):
    def __init__(self, lineno, message):
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


ESCAPES = {'"': '"', "\\": "\\", "n": "\n"}


def _quoted(rest, lineno):
    out = []
    i = 1
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            if i + 1 >= len(rest) or rest[i + 1] not in ESCAPES:
                raise ParseError(lineno, "invalid escape")
            out.append(ESCAPES[rest[i + 1]])
            i += 2
            continue
        if ch == '"':
            tail = rest[i + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise ParseError(lineno, "text after closing quote")
            return "".join(out)
        out.append(ch)
        i += 1
    raise ParseError(lineno, "missing closing quote")


def parse(text):
    result = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in raw:
            raise ParseError(lineno, "missing '='")
        key, rest = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ParseError(lineno, "empty key")
        rest = rest.lstrip()
        if rest.startswith('"'):
            value = _quoted(rest, lineno)
        else:
            value = rest.split("#", 1)[0].strip()
        result[key] = value
    return result
