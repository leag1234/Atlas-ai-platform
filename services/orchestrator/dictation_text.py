"""Dictation text rules that never depend on a model: hesitations and a language guess.

Dictation is written "clean verbatim": hesitation sounds are removed, every real word is
kept. The removal is a fixed rule per language (owner decision 2026-11-17), not a model
choice: the model clean-up removed French "euh" but kept English "um", and it may no longer
delete words at all (dictation_cleanup.accepted).

A sound is listed for one language only when it is a word of another language Atlas
dictates: "um" is Portuguese for "one", "em" Portuguese for "in", "eh" starts the French
"eh bien" and "hum" is an English word ("mm", a unit, and "er" are never removed). Those are
removed only when the dictation language is known: set by the user, reported by the
engine, or guessed from the text with enough evidence.
"""

import re
import unicodedata
from functools import lru_cache

# Matched case-insensitively, any letter repeated ("euuuh", "hmmm", "ähhm").
COMMON = (
    "euh",
    "heu",
    "uh",
    "uhm",
    "ehm",
    "ähm",
    "äh",
    "öhm",
    "öh",
    "hm",
    "mmh",
    "erm",
)
BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "fr": ("hum",),
    "en": ("um",),
    "nl": ("eh",),
    "es": ("eh", "em"),
    "it": ("eh",),
}
# Function words of one language: enough of them name the language of a text.
STOP_WORDS: dict[str, frozenset[str]] = {
    "fr": frozenset(
        "le la les des une est et que qui pas pour dans sur avec je nous vous il elle "
        "ce cette".split()
    ),
    "en": frozenset(
        "the and is are was that this with for you we they have not of to it".split()
    ),
    "de": frozenset(
        "der die das und ist nicht ich wir sie mit für auf ein eine zu den dem".split()
    ),
    "es": frozenset(
        "el los las y es que una por para con no se lo del al está".split()
    ),
    "it": frozenset(
        "il lo gli e è che una per con non di della sono anche nel".split()
    ),
    "nl": frozenset("de het een en is van niet dat op met voor ik wij zijn".split()),
    "pt": frozenset("o os as e é que uma para com não do da em um são".split()),
}
_MARK = (
    "\x00"  # where a removed sound started a sentence: the next word takes a capital
)


def hesitations(language: str | None) -> tuple[str, ...]:
    return COMMON + BY_LANGUAGE.get(language or "", ())


def _spelling(sound: str) -> str:
    return "".join(re.escape(letter) + "+" for letter in sound)


@lru_cache(maxsize=16)
def _regex(language: str | None) -> re.Pattern[str]:
    sounds = sorted(hesitations(language), key=len, reverse=True)
    return re.compile(
        r"(?P<pre>[,;]?)\s*(?<!\w)(?P<sound>"
        + "|".join(_spelling(sound) for sound in sounds)
        + r")(?!\w)(?P<post>\s*(?:\.\.\.|…|[,;.!?])?)",
        re.IGNORECASE,
    )


def is_hesitation(word: str, language: str | None = None) -> bool:
    """One word (lower case, no punctuation) is a hesitation sound."""
    return any(
        re.fullmatch(_spelling(sound), word, re.IGNORECASE)
        for sound in hesitations(language)
    )


def guess_language(text: str) -> str | None:
    """The language whose function words dominate the text; None without enough evidence."""
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", text).lower())
    ranked = sorted(
        (
            (sum(word in stop for word in words), code)
            for code, stop in STOP_WORDS.items()
        ),
        reverse=True,
    )
    (best, code), (runner, _) = ranked[0], ranked[1]
    return code if best >= 3 and best >= 2 * runner else None


def remove_hesitations(text: str, language: str | None = None) -> str:
    """The text without hesitation sounds; its words and sentence punctuation are kept.

    ``language`` is the dictation language when known, else it is guessed from the text.
    """
    if not text:
        return text
    language = language or guess_language(text)
    out: list[str] = []
    position = 0
    for match in _regex(language).finditer(text):
        out.append(text[position : match.start()])
        position = match.end()
        pre, post = match["pre"], match["post"].strip()
        if post in {".", "!", "?"}:
            out.append(post)  # "je pense euh." keeps its full stop
        elif pre:
            out.append(pre)  # "Bonjour, euh, je" keeps the comma after "Bonjour"
        out.append(" ")
        before = "".join(out).rstrip()
        if not before or before[-1] in ".!?":
            out.append(_MARK)
    if not out:
        return text
    out.append(text[position:])
    result = re.sub(
        _MARK + r"[\s" + _MARK + r"]*(\w)",
        lambda found: found[1].upper(),
        "".join(out),
    ).replace(_MARK, "")
    result = re.sub(r"[ \t]+([,;.!?…])", r"\1", result)
    result = re.sub(r"[ \t]{2,}", " ", result).strip()
    return re.sub(r"^[,;.]\s*", "", result)
