import unicodedata

from textkit._deprecated import alias


def strip_accents(text):
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


stripAccents = alias(strip_accents, "stripAccents")
