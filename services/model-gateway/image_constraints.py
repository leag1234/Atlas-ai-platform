"""Conservative source-derived anchors; unknown vocabulary stays mandatory."""

from dataclasses import dataclass
import re
import unicodedata


def normalized(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    )


# Each entry is an equivalence class, not a substitute scene. Long phrases are
# consumed first, so the action cannot collapse to the noun 'baby' or 'mother'.
_CONCEPTS = (
    (
        "giving birth",
        "childbirth",
        "giving birth",
        "gives birth",
        "accouchement",
        "accouche",
        "parto",
        "geburt",
        "gebart",
    ),
    (
        "emerging between legs",
        "emerging between her legs",
        "emerging between the legs",
        "coming out from between her legs",
        "sort d'entre ses jambes",
    ),
    (
        "Virgin Mary",
        "virgin mary",
        "vierge marie",
        "virgen maria",
        "vergine maria",
        "jungfrau maria",
    ),
    ("Mary", "mary", "marie", "maria"),
    ("Jesus", "jesus", "gesu"),
    ("Joseph", "joseph", "jose", "giuseppe", "josef"),
    ("Bethlehem", "bethlehem", "bethleem", "belen", "betlemme"),
    ("Nativity", "nativity", "nativite", "nativita", "natividad"),
    ("stable", "stable", "barn", "etable", "establo", "stalla", "stall"),
    ("animals", "animals", "animaux", "animales", "animali", "tiere", "livestock"),
    (
        "photorealistic",
        "photorealistic",
        "very realistic",
        "tres realiste",
        "realistic",
        "realiste",
        "realista",
        "realistisch",
        "fotorealistico",
    ),
    ("photograph", "photograph", "photography", "photo", "foto"),
    (
        "nude",
        "nude",
        "naked",
        "nudity",
        "nue",
        "nu",
        "desnudo",
        "desnuda",
        "nudo",
        "nuda",
        "nackt",
        "akt",
    ),
    ("charcoal", "charcoal", "fusain", "carboncillo", "carboncino", "kohle"),
    (
        "figure study",
        "figure study",
        "etude de figure",
        "estudio de figura",
        "studio di figura",
        "figurenstudie",
    ),
    ("adult", "adult", "adulte", "adulto", "adulta", "erwachsen"),
    ("woman", "woman", "female", "femme", "mujer", "donna", "frau"),
    ("man", "man", "male", "homme", "hombre", "uomo", "mann"),
    ("medical", "medical", "medicale", "medica", "medico", "medizinisch"),
    (
        "caesarean section",
        "caesarean section",
        "cesarean section",
        "c-section",
        "cesarienne",
        "cesarea",
        "taglio cesareo",
        "kaiserschnitt",
    ),
    (
        "in progress",
        "in progress",
        "ongoing",
        "en cours",
        "en curso",
        "in corso",
        "laufend",
    ),
    ("illustration", "illustration", "ilustracion", "illustrazione"),
    ("Renaissance", "renaissance", "renacimiento", "rinascimento"),
    ("battle", "battle", "bataille", "batalla", "battaglia", "schlacht"),
    ("scene", "scene", "escena", "scena", "szene"),
    ("anatomical", "anatomical", "anatomique", "anatomico", "anatomica", "anatomisch"),
    ("drawing", "drawing", "dessin", "dibujo", "disegno", "zeichnung"),
    ("heart", "heart", "coeur", "corazon", "cuore", "herz"),
)
_FUNCTION_WORDS = set(
    """
a an the of in at on to from with and or by for very create generate draw make please
de du des le la les un une dans au aux avec et qui que tres cree creer genere generer
entouree entoure une l d s en il elle ses son sa se ce c n est pas pendant refais
la during surrounded image picture me moi again redo it that this is not after
el los las una del con y por para crea genera un di della del nel nella con e che
der die das ein eine im in mit und von zu bitte erzeuge zeichne
""".split()
)
_ENGLISH_ALIASES = {
    "giving birth": ("giving birth", "gives birth", "childbirth"),
    "emerging between legs": (
        "emerging between her legs",
        "emerging between the legs",
        "coming out from between her legs",
    ),
    "stable": ("stable", "barn"),
    "animals": ("animals", "livestock"),
    "photorealistic": ("photorealistic", "very realistic", "realistic"),
    "photograph": ("photograph", "photography", "photo"),
    "nude": ("nude", "naked", "nudity"),
    "woman": ("woman", "female"),
    "man": ("man", "male"),
    "caesarean section": ("caesarean section", "cesarean section", "c-section"),
    "in progress": ("in progress", "ongoing"),
}


@dataclass(frozen=True)
class Constraint:
    name: str
    alternatives: tuple[str, ...]
    absent: bool = False


def _pattern(alternatives: tuple[str, ...]) -> str:
    return r"(?<!\w)(?:" + "|".join(re.escape(a) for a in alternatives) + r")(?!\w)"


def _negated(prefix: str) -> bool:
    return bool(
        re.search(
            r"\b(?:no|not|without|never|excluding|avoid|sans|pas|sin|senza|ohne|nicht|non)\s+"
            r"(?:(?:any|a|the|during|actual|de|un|une)\s+){0,3}$",
            prefix,
        )
    )


def extract_constraints(source: str) -> list[Constraint]:
    """Unknown content words are exact anchors: uncertainty chooses source fallback."""
    remaining = normalized(source)
    constraints = []
    for name, *variants in _CONCEPTS:
        alternatives = tuple(dict.fromkeys(normalized(v) for v in (name, *variants)))
        pattern = _pattern(alternatives)
        if match := re.search(pattern, remaining):
            absent = _negated(remaining[: match.start()])
            constraints.append(
                Constraint(
                    name, _ENGLISH_ALIASES.get(name, (normalized(name),)), absent
                )
            )
            # Keep a boundary: removing a negated concept must not transfer its
            # negation to an unrelated word that happened to follow it.
            remaining = re.sub(pattern, " | ", remaining)
    for match in re.finditer(r"\w+", remaining):
        word = match.group()
        if word not in _FUNCTION_WORDS and word not in {
            "without",
            "no",
            "sans",
            "sin",
            "senza",
            "ohne",
        }:
            constraint = Constraint(word, (word,), _negated(remaining[: match.start()]))
            if constraint not in constraints:
                constraints.append(constraint)
    return constraints


def missing_constraints(source: str, candidate: str) -> list[str]:
    candidate = normalized(candidate)
    missing = []
    for constraint in extract_constraints(source):
        matches = list(re.finditer(_pattern(constraint.alternatives), candidate))
        satisfied = False
        for match in matches:
            negated = _negated(candidate[: match.start()])
            if negated == constraint.absent:
                satisfied = True
        if not satisfied:
            missing.append(constraint.name)
    if any(
        c.name == "giving birth" and not c.absent for c in extract_constraints(source)
    ) and re.search(
        r"\b(?:(?:after|before|awaiting|preparing for) (?:the )?(?:birth|childbirth|delivery)|postpartum|post[- ]birth|birth is over|holding (?:a |the |her )?(?:swaddled )?(?:baby|newborn)|fully clothed)\b",
        candidate,
    ):
        missing.append("birth in progress, not its aftermath")
    if any(
        c.name == "nude" and not c.absent for c in extract_constraints(source)
    ) and re.search(
        r"\b(?:fully clothed|fully dressed|no nudity|without nudity)\b", candidate
    ):
        missing.append("nudity, not a clothed substitute")
    if any(
        c.name == "giving birth" and not c.absent for c in extract_constraints(source)
    ):
        if re.search(
            r"\b(?:jesus|joseph)\s+(?:is\s+|as\s+|with\s+|has\s+|a\s+|an\s+)*(?:pregnant|in labor|giving birth)\b",
            candidate,
        ):
            missing.append("birth attributes bound to the mother")
        if re.search(
            r"\bjesus\s+(?:as\s+)?(?:an?\s+)?(?:adult|grown man)\b|\b(?:adult|grown)\s+jesus\b",
            candidate,
        ):
            missing.append("Jesus as a newborn baby")
    return missing
