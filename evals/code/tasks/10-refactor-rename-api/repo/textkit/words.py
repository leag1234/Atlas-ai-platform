import re

WORD = re.compile(r"[\w']+")


def wordCount(text):
    return len(WORD.findall(text))


def truncateWords(text, limit, suffix="..."):
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + suffix
