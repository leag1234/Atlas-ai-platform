"""Sentence-level removal of unsupported future file actions, FR/EN/DE/ES/IT."""

import re
import unicodedata


def _folded(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    ).replace("’", "'")


# First-person future/intent markers plus file verbs/objects, or an explicit retry.
# Kept separate from is_promise_only: substantive prose must not mask an action
# that cannot happen after the final turn. Ordinary explanatory futures survive.
_FILE_FUTURE = re.compile(
    r"\b(?:i will|i'll|i am going to|i'm going to|we will|we'll|let me|"
    r"je vais|je compte|je procede|nous allons|ich werde|wir werden|voy a|vamos a|"
    r"procedero a|creero|generero)\b[^.!?;\n]{0,180}"
    r"\b(?:retry|try again|reessayer|retenter|erneut|noch einmal|reintentar|riprovare|"
    r"create|generate|publish|save|export|creer|generer|publier|integrer|erstellen|crear|creare|"
    r"file|document\w*|pdf|pptx|docx|xlsx|fichier\w*|datei|archivo)\b"
)


def strip_file_promises(text: str) -> str:
    """Remove complete promising sentences, retaining content and disclosure."""
    parts = re.split(r"(?<=[.!?;])(?=\s)|(?<=\n)", text)
    kept = [part for part in parts if not _FILE_FUTURE.search(_folded(part))]
    return "".join(kept).strip() if len(kept) != len(parts) else text
