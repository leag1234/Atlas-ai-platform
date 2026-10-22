"""Conservative deterministic detection of intent-only assistant final answers."""

import re
import unicodedata

from packages.file_answers import strip_file_promises as strip_file_promises


_INTENTS = (
    "i will ",
    "i'll ",
    "i am going to ",
    "i'm going to ",
    "let me ",
    "i plan to ",
    "we will ",
    "we'll ",
    "je vais ",
    "je compte ",
    "nous allons ",
    "ich werde ",
    "voy a ",
    "vamos a ",
    "procedero a ",
)
_ACK = re.compile(r"^(?:sure|certainly|okay|ok|bien sur|d'accord)[,.!:\s]+")
_FAILURE = re.compile(
    r"^(?:(?:the )?file (?:operation failed|could not be produced)|"
    r"le fichier n'a pas pu etre|la creation du fichier a echoue)"
)


def normalized(text: str) -> str:
    return " ".join(
        "".join(
            char
            for char in unicodedata.normalize("NFKD", text.casefold())
            if not unicodedata.combining(char)
        )
        .replace("’", "'")
        .split()
    ).lstrip("* ")


def possible_promise_answer(text: str) -> bool:
    """Hold a fragmented intent prefix, then its whole draft until validation."""
    value = _ACK.sub("", normalized(text))
    return any(
        phrase.startswith(value) or value.startswith(phrase)
        for phrase in (
            *_INTENTS,
            "sure",
            "certainly",
            "okay",
            "ok",
            "bien sur",
            "d'accord",
        )
    )


def missing_file_failure(text: str) -> bool:
    return (
        re.search(
            r"could not|cannot|can't|failed|failure|unable|"
            r"pas pu|impossible|echec|echoue|ne (?:peux|peut) pas|"
            r"fehlgeschlagen|konnte nicht|nicht erstellt|"
            r"no (?:se )?pudo|fallo|non (?:e|ho) (?:stato |potuto )?",
            normalized(text),
        )
        is None
    )


def is_promise_only(text: str) -> bool:
    """Reject recognizable intentions only; substantive follow-up content survives.

    This is a delivery check, not a factual or completeness judge. Code, quotations
    and ordinary future-tense explanations must not be mistaken for tool promises.
    """
    if "```" in text:
        return False
    parts = re.split(r"[.!?;:\n]+\s*", text.strip())
    promise = False
    for part in parts:
        value = _ACK.sub("", normalized(part))
        if not value or value in {
            "sure",
            "certainly",
            "okay",
            "ok",
            "bien sur",
            "d'accord",
        }:
            continue
        if any(value.startswith(phrase) for phrase in _INTENTS):
            promise = True
        elif not _FAILURE.match(value):
            return False
    return promise


ANSWER_NOW = (
    "The previous draft contained only intentions and was not delivered. "
    "Provide the requested substantive answer now, in the user's language, "
    "using the evidence already available. Tools are disabled on this final turn. "
    "Do not promise to search, create or publish anything later. "
    "If evidence is missing, explain the uncertainty and give the supported analysis; "
    "never invent sources. If a file operation failed, explicitly say the file "
    "could not be produced and supply the complete requested content inline, "
    "preserving the requested structure and number of items. "
    "A script that would generate a document is not the document content. "
    "Return prose directly unless the user explicitly requested code. "
    "An apology or a plan alone is not an answer."
)


def failed_terminal_content(text: str, messages: list[dict[str, object]]) -> bool:
    """A file-generation script is not the requested prose after tool failure."""
    question = next(
        (
            str(m.get("content", ""))
            for m in reversed(messages)
            if m.get("role") == "user"
        ),
        "",
    )
    code_requested = re.search(
        r"\b(?:script|code|python|sql|function|fonction|programm)\b",
        normalized(question),
    )
    if not code_requested and (
        "```" in text
        or re.search(
            r"(?m)^\s*(?:from \w+ import |import \w+|def \w+\(|doc\.add_|print\()", text
        )
    ):
        return True
    prose = re.sub(r"```.*?(?:```|$)", "", text, flags=re.S)
    parts = re.split(r"[.!?;:\n]+\s*", prose.strip())
    substantive = []
    for part in parts:
        value = _ACK.sub("", normalized(part)).strip()
        if not value or any(value.startswith(intent) for intent in _INTENTS):
            continue
        if re.search(
            r"could not|cannot|failed|failure|unable|sorry|apologi|pas pu|impossible|echou|echec|desole|excuse|fehlgeschlagen|konnte nicht|leider|lo siento|no pudo|fallo|mi dispiace|non ho potuto",
            value,
        ):
            continue
        substantive.append(value)
    return not substantive and not (code_requested and "```" in text)
