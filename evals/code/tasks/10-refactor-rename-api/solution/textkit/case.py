import re

from textkit._deprecated import alias
from textkit.normalize import strip_accents


def to_snake(text):
    text = strip_accents(text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return text.strip("_").lower()


toSnake = alias(to_snake, "toSnake")
