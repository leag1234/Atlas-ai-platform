"""Classify transformations only when a previous assistant answer exists."""

import re

from services.orchestrator.chat_schema import ChatMessage


def is_followup(messages: list[ChatMessage]) -> bool:
    if len(messages) < 2 or messages[-2].role != "assistant":
        return False
    text = messages[-1].text.strip()
    return bool(
        re.fullmatch(
            r"(?is)(?:plus court|shorter|en allemand|in German|en anglais)[.!?]*",
            text,
        )
        or (
            re.search(
                r"(?i)\b(tradui\w*|translat\w*|résum\w*|summari[sz]\w*|reformul\w*|rewrite)\b",
                text,
            )
            and re.search(
                r"(?i)\b(ta réponse|votre réponse|ce que tu|précédent\w*|ça|cela|that|it|your answer|previous)\b",
                text,
            )
        )
    )


# Only generated-image references establish a thread, never an image mention.
_IMAGE_CONTEXT = re.compile(r"!\[[^\]]*\]\([^)]*/images/[^)]+\)", re.IGNORECASE)
_IMAGE_EXPLANATION = re.compile(
    r"\b(?:en quoi|pourquoi|why|warum|por qu[eé]|perch[eé])\b.{0,120}"
    r"\b(?:bloqu\w*|probl[eè]m\w*|problem\w*|cannot|can't|refus\w*|interdit|"
    r"forbid\w*|block\w*|verbot\w*|prohib\w*|viet\w*)\b|"
    r"\b(?:explain|describe|expliqu\w*|d[eé]cris|beschreib\w*|erkl[aä]r\w*|"
    r"explica\w*|spiega\w*|descrivi)\b|"
    r"\b(?:how|comment|wie|c[oó]mo|come)\b.{0,65}"
    r"\b(?:works?|fonctionn\w*|funktionier\w*|funciona\w*|funziona\w*)\b|"
    r"\b(?:do not|don't|ne|nicht|no|non)\s+"
    r"(?:generate|create|draw|g[eé]n[eè]re|cr[eé]e|dessine|generier\w*|"
    r"erzeug\w*|genera\w*|crea\w*|dibuja\w*|disegna\w*)\b|"
    r"\b(?:password|passwort|contrase[nñ]a|mot de passe|account|weather|m[eé]t[eé]o|wetter|"
    r"write|calculate|translate|summarize|[eé]cris|calcule|traduis|"
    r"schreib\w*|berechne|[uü]bersetz\w*|escribe|calcula|traduce|scrivi|traduci)\b",
    re.IGNORECASE,
)
_IMAGE_CORRECTION = re.compile(
    r"\b(?:trois mains|three hands|drei h[aä]nde|tres manos|tre mani|"
    r"extra (?:hands|limbs)|too many hands|trop de mains|zu viele h[aä]nde|"
    r"demasiadas manos|troppe mani|il manque|on ne voit|je ne vois|"
    r"i (?:cannot|can't|don't) see|ich sehe|no se ve|non si vede|"
    r"not what i (?:asked|requested)|nicht was ich|no es lo que|non [eè] quello)\b|"
    r"\b(?:ajout\w*|oubli\w*|m[eê]me|refais(?:-la|-le)?|recommence|"
    r"add|forgot|same|change|remplac\w*|redo|again|regenerate|missing|"
    r"nochmal|erneut|wieder|fehlt|hinzuf[uü]g\w*|statt|"
    r"rehaz\w*|otra vez|falta\w*|a[nñ]ade|en vez|"
    r"rifa\w*|di nuovo|manca\w*|aggiungi|invece)\b|"
    r"\b(?:ce n['’]est pas|c['’]est apr[eè]s|pas pendant|"
    r"(?:that|this|it) (?:is not|isn't|is after|shows)|"
    r"das ist nicht|nicht w[aä]hrend|no es durante|non [eè] durante)\b",
    re.IGNORECASE,
)
_IMAGE_RETRY_PROPOSAL = re.compile(
    r"\b(?:retry|try again|one last try|refine|detailed description|"
    r"r[eé]essa(?:i|y)|nouvelle tentative|description d[eé]taill[eé]e|"
    r"noch einmal|erneut|int[eé]ntalo de nuevo)\b",
    re.IGNORECASE,
)
_IMAGE_CONFIRMATION = re.compile(
    r"(?is)^\s*(?:yes|yeah|yep|oui|si|ja|s[iì]|d'accord|ok|okay|"
    r"propose(?:-moi)?(?: cette)? description)\b.*$",
)
_ANAPHORIC_ITERATION = re.compile(
    r"(?is)^\s*same\s+but\b",
)
_IMAGE_LEAD_IN = re.compile(
    r"(?is)^\s*(?:please\s+)?(?:generate|create|make|draw|paint|"
    r"g[eé]n[eè]re|cr[eé]e|dessine|fais|erzeuge|generiere|zeichne|"
    r"genera|crea|dibuja|disegna)\s+(?:me\s+)?(?:an?\s+|une?\s+|un\s+)?"
)
_NUMBER_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
    "10": "ten",
    "11": "eleven",
    "12": "twelve",
}


def image_correction(text: str) -> bool:
    return bool(_IMAGE_CORRECTION.search(text) and not _IMAGE_EXPLANATION.search(text))


def image_retry_confirmation(messages: list[ChatMessage]) -> bool:
    """Recognize agreement to the assistant's offered image retry, not bare assent."""
    return bool(
        len(messages) >= 2
        and messages[-2].role == "assistant"
        and _IMAGE_CONFIRMATION.fullmatch(messages[-1].text.strip())
        and _IMAGE_RETRY_PROPOSAL.search(messages[-2].text)
    )


def raw_scene_continuation(messages: list[ChatMessage]) -> bool:
    """Keep the established gateway input for a terse inherited-scene edit."""
    return bool(messages and _ANAPHORIC_ITERATION.match(messages[-1].text.strip()))


def image_explanation(text: str) -> bool:
    return bool(_IMAGE_EXPLANATION.search(text))


def image_dialogue(messages: list[ChatMessage]) -> list[str]:
    """Collect user-owned history independently of how generation was selected."""
    if not messages or messages[-1].role != "user":
        return []
    start = len(messages) - 1
    candidate = start
    while candidate >= 2 and messages[candidate - 1].role == "assistant":
        previous = messages[candidate - 2]
        if previous.role != "user":
            break
        if _IMAGE_CONTEXT.search(
            messages[candidate - 1].text
        ) or _IMAGE_RETRY_PROPOSAL.search(messages[candidate - 1].text):
            start = candidate - 2
        elif not image_explanation(previous.text):
            break
        candidate -= 2
    turns = [m.text for m in messages[start:] if m.role == "user"]
    if len(turns) > 24:
        turns = [turns[0], *turns[-23:]]
    # Trim the oldest edits first, retaining the original and newest request.
    while len(turns) > 2 and len(" ".join(turns)) > 2000:
        turns.pop(1)
    if len(turns) > 1 and len(" ".join(turns)) > 2000:
        available = 2000 - len(turns[0]) - 1
        if available <= 0:
            raise ValueError("image_dialogue_budget")
        turns[-1] = turns[-1][:available].rstrip()
    return turns


def image_iteration(messages: list[ChatMessage]) -> str | None:
    """A direct-route hint; never the gate for carrying image history."""
    if not messages or not (
        image_correction(messages[-1].text) or image_retry_confirmation(messages)
    ):
        return None
    turns = image_dialogue(messages)
    return "\n".join(turns) if len(turns) > 1 else None


def visual_image_prompt(dialogue: list[str]) -> str:
    """Turn user-owned scene requests into a bounded prompt for an image model.

    The confirmation itself and every assistant turn are deliberately excluded: image
    workers render visual instructions, never a conversation transcript.
    """
    if not dialogue:
        raise ValueError("image_prompt_missing")
    source = _IMAGE_LEAD_IN.sub("", dialogue[0]).strip(" .:!?") or dialogue[0].strip()
    refinements = [
        turn.strip(" .:!?")
        for turn in dialogue[1:]
        if turn.strip() and not _IMAGE_CONFIRMATION.fullmatch(turn.strip())
    ]
    source = re.sub(
        r"\b(\d{1,2})\b",
        lambda match: _NUMBER_WORDS.get(match.group(1), match.group(1)),
        source,
    )
    parts = [
        "Visual image only.",
        "Subject and required details: " + source + ".",
    ]
    if refinements:
        parts.append("Agreed visual refinements: " + "; ".join(refinements) + ".")
    legs = re.search(r"\b(?:eight|8)\s+legs\b", source, re.IGNORECASE)
    if legs:
        parts.append("Layout: four pairs of legs clearly visible.")
    parts.extend(
        (
            "Composition: make every requested subject and quantity clearly visible.",
            "No text, lettering, icons, interface elements, or app-like layout.",
        )
    )
    prompt = " ".join(parts)
    if len(prompt) > 1200:
        raise ValueError("image_prompt_too_long")
    return prompt
