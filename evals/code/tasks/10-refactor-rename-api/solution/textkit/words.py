import re

from textkit._deprecated import alias

WORD = re.compile(r"[\w']+")


def word_count(text):
    return len(WORD.findall(text))


def truncate_words(text, limit, suffix="..."):
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + suffix


wordCount = alias(word_count, "wordCount")
truncateWords = alias(truncate_words, "truncateWords")
