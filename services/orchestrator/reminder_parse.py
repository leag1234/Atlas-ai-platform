"""Reminder requests read by code: trigger, date, time, recurrence and early alarm.

The model never computes a date (roadmap T8). A fixed grammar in the five
interface languages reads what the person wrote, tolerant of missing accents,
missing punctuation and one-letter typos in the key words; the clock and the
calendar arithmetic run on the person's IANA zone. Whatever is missing or
unclear (no time, "ce soir", a moment already past) becomes one short question.
dateparser, already installed through trafilatura, was measured on the reference
phrasings and rejected (docs/25-reminders.md).
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from services.orchestrator.reminder_ics import Recurrence

LANGUAGES = ("fr", "en", "de", "es", "it")
MAX_TEXT = 400
MAX_TITLE = 120


def fold(text: str) -> str:
    """Lower case without accents, one character for one character."""
    out: list[str] = []
    for char in text:
        if char in "’‘ʼ`´":
            out.append("'")
            continue
        low = char.lower()
        if len(low) != 1:
            low = char
        kept = [
            c
            for c in unicodedata.normalize("NFKD", low)
            if not unicodedata.combining(c)
        ]
        out.append(kept[0] if kept and kept[0].isprintable() else low)
    return "".join(out)


# --------------------------------------------------------------- vocabulary

NUMBERS = {
    "un": 1, "une": 1, "one": 1, "a": 1, "an": 1, "ein": 1, "eine": 1,
    "einer": 1, "einem": 1, "einen": 1, "uno": 1, "una": 1,
    "deux": 2, "two": 2, "zwei": 2, "dos": 2, "due": 2,
    "trois": 3, "three": 3, "drei": 3, "tres": 3, "tre": 3,
    "quatre": 4, "four": 4, "vier": 4, "cuatro": 4, "quattro": 4,
    "cinq": 5, "five": 5, "funf": 5, "fuenf": 5, "cinco": 5, "cinque": 5,
    "six": 6, "sechs": 6, "seis": 6, "sei": 6,
    "sept": 7, "seven": 7, "sieben": 7, "siete": 7, "sette": 7,
    "huit": 8, "eight": 8, "acht": 8, "ocho": 8, "otto": 8,
    "neuf": 9, "nine": 9, "neun": 9, "nueve": 9, "nove": 9,
    "dix": 10, "ten": 10, "zehn": 10, "diez": 10, "dieci": 10,
    "onze": 11, "eleven": 11, "elf": 11, "once": 11, "undici": 11,
    "douze": 12, "twelve": 12, "zwolf": 12, "doce": 12, "dodici": 12,
    "quinze": 15, "fifteen": 15, "funfzehn": 15, "quince": 15, "quindici": 15,
    "vingt": 20, "twenty": 20, "zwanzig": 20, "veinte": 20, "venti": 20,
    "trente": 30, "thirty": 30, "dreissig": 30, "treinta": 30, "trenta": 30,
}  # fmt: skip
# Hour words after a clear time preposition ("at nine", "um neun", "a las nueve").
HOUR_WORDS = {
    word: value
    for word, value in NUMBERS.items()
    if 2 <= value <= 12 and word not in {"tres", "sei", "due"}
}
HOUR_WORDS.update({"one": 1, "una": 1})


def _words(values: dict[str, int]) -> str:
    return "|".join(sorted(values, key=len, reverse=True))


QTY = rf"(?:\d{{1,4}}|{_words(NUMBERS)})"
HOURW = rf"(?:\d{{1,2}}|{_words(HOUR_WORDS)})"
MINUTE_UNIT = r"(?:minutes?|minuten|minutos?|minuti|mins?|mn)"
UNITS = {
    "minutes": MINUTE_UNIT,
    "hours": r"(?:heures?|hours?|hrs?|stunden?|std|horas?|ore|ora|h)",
    "days": r"(?:journees?|jours?|days?|tagen?|tage|tag|dias?|giorni|giorno)",
    "weeks": r"(?:semaines?|weeks?|wochen?|semanas?|settimane|settimana)",
    "months": r"(?:mois|months?|monaten?|monate|monat|meses|mes|mesi|mese)",
}
ANY_UNIT = "(?:" + "|".join(UNITS.values()) + ")"

WEEKDAYS: dict[str, int] = {}
for _index, _names in enumerate(
    (
        ("lundi", "monday", "montag", "lunes", "lunedi"),
        ("mardi", "tuesday", "dienstag", "martes", "martedi"),
        ("mercredi", "wednesday", "mittwoch", "miercoles", "mercoledi"),
        ("jeudi", "thursday", "donnerstag", "jueves", "giovedi"),
        ("vendredi", "friday", "freitag", "viernes", "venerdi"),
        ("samedi", "saturday", "samstag", "sonnabend", "sabado", "sabato"),
        ("dimanche", "sunday", "sonntag", "domingo", "domenica"),
    )
):
    for _name in _names:
        WEEKDAYS[_name] = _index
WD = rf"(?:{_words(WEEKDAYS)})"

MONTHS: dict[str, int] = {}
for _index, _names in enumerate(
    (
        ("janvier", "janv", "january", "jan", "januar", "enero", "gennaio"),
        ("fevrier", "fevr", "february", "feb", "februar", "febrero", "febbraio"),
        ("mars", "march", "marz", "maerz", "marzo"),
        ("avril", "avr", "april", "apr", "abril", "aprile"),
        ("mai", "may", "mayo", "maggio"),
        ("juin", "june", "jun", "juni", "junio", "giugno"),
        ("juillet", "juil", "july", "jul", "juli", "julio", "luglio"),
        ("aout", "august", "aug", "agosto"),
        ("septembre", "sept", "september", "sep", "septiembre", "setiembre",
         "settembre"),
        ("octobre", "oct", "october", "oktober", "octubre", "ottobre"),
        ("novembre", "nov", "november", "noviembre"),
        ("decembre", "dec", "december", "dezember", "diciembre", "dicembre"),
    ),
    start=1,
):  # fmt: skip
    for _name in _names:
        MONTHS[_name] = _index
MONTH = rf"(?:{_words(MONTHS)})"

PARTS = {
    "morning": ("matin", "matinee", "morning", "mornings", "morgens", "vormittag",
                "vormittags", "fruh", "manana", "mananas", "mattina", "mattino",
                "mattine", "stamattina", "stamani", "matins", "morgen"),
    "afternoon": ("apres-midi", "apres midi", "afternoon", "nachmittag",
                  "nachmittags", "tarde", "tardes", "pomeriggio", "mittag"),
    "evening": ("soir", "soirs", "soiree", "evening", "evenings", "abend", "abends",
                "noche", "noches", "sera", "sere", "stasera", "tonight"),
    "night": ("nuit", "night", "nights", "nacht", "nachts", "notte", "stanotte"),
}  # fmt: skip
PART_OF = {word: part for part, words in PARTS.items() for word in words}
# Daypart words that are other words in another interface language.
PART_LANGUAGE = {"sera": "it", "sere": "it", "tarde": "es", "tardes": "es"}

# Words a one-letter typo is corrected to: the trigger verbs in every language,
# the date words of the request's language.
TRIGGER_WORDS = (
    "rappelle", "rappeler", "rappelez", "remind", "reminder", "erinnere",
    "erinnern", "erinnerung", "recuerdame", "recordarme", "recordatorio",
    "ricordami", "ricordarmi", "promemoria",
)  # fmt: skip
VOCABULARY = {
    "fr": ("demain", "aujourd'hui", "mercredi", "vendredi", "samedi", "dimanche",
           "minutes", "heures", "semaine", "prochain", "prochaine", "chaque",
           "toutes", "janvier", "fevrier", "juillet", "septembre", "octobre",
           "novembre", "decembre"),
    "en": ("tomorrow", "today", "tonight", "monday", "tuesday", "wednesday",
           "thursday", "friday", "saturday", "sunday", "minutes", "before",
           "january", "february", "august", "september", "october", "november",
           "december", "morning", "afternoon", "evening"),
    "de": ("morgen", "ubermorgen", "montag", "dienstag", "mittwoch", "donnerstag",
           "freitag", "samstag", "sonntag", "minuten", "stunden", "nachsten",
           "vorher", "januar", "februar", "oktober", "dezember", "nachmittag"),
    "es": ("manana", "pasado", "miercoles", "jueves", "viernes", "sabado",
           "domingo", "minutos", "proximo", "febrero", "septiembre", "octubre",
           "noviembre", "diciembre"),
    "it": ("domani", "dopodomani", "lunedi", "martedi", "mercoledi", "giovedi",
           "venerdi", "sabato", "domenica", "minuti", "prossimo", "prossima",
           "gennaio", "febbraio", "settembre", "ottobre", "novembre", "dicembre",
           "mattina", "pomeriggio", "stasera"),
}  # fmt: skip
KNOWN = (
    set(NUMBERS)
    | set(WEEKDAYS)
    | set(MONTHS)
    | set(PART_OF)
    | {
        "morgens", "abends", "montags", "dienstags", "mittwochs", "donnerstags",
        "freitags", "samstags", "sonntags", "minute", "heure", "hours", "hour",
        "stunde", "minuto", "minutes", "remain", "domain", "matins", "soirs",
    }
)  # fmt: skip


def _one_edit(word: str, target: str) -> bool:
    """Damerau-Levenshtein distance of exactly one."""
    if word == target or abs(len(word) - len(target)) > 1:
        return False
    if len(word) == len(target):
        diff = [i for i, (a, b) in enumerate(zip(word, target, strict=True)) if a != b]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and word[diff[0]] == target[diff[1]]
            and word[diff[1]] == target[diff[0]]
        )
    short, long_ = (word, target) if len(word) < len(target) else (target, word)
    for index in range(len(long_)):
        if long_[:index] + long_[index + 1 :] == short:
            return True
    return False


def _corrected(word: str, vocabulary: tuple[str, ...]) -> str:
    if (
        len(word) < 5
        or word in vocabulary
        or word in KNOWN
        or word.rstrip("s") in KNOWN
    ):
        return word
    for target in vocabulary:
        if len(target) >= 6 and "'" not in target and _one_edit(word, target):
            return target
    return word


# ----------------------------------------------------------------- triggers

TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (language, re.compile(pattern))
    for language, pattern in (
        ("fr", r"\brap+el+(?:le|e|ez|er)?[\s-]*+(?:moi|nous)\b"),
        ("fr", r"\b(?:me|nous)\s+rap+el+(?:er|eras|erais|erez|eriez)\b"),
        ("fr", r"\bfai(?:s|tes)[\s-]*+(?:moi|nous)\s+penser\b"),
        ("fr", r"\b(?:me|nous)\s+faire\s+penser\b"),
        (
            "fr",
            r"\b(?:cree|creer|creez|mets|mettre|mettez|met|ajoute|ajouter|ajoutez|"
            r"programme|programmer|programmez|planifie|planifier|fixe|pose|"
            r"enregistre|configure)(?:[\s-]*+(?:moi|nous))?\s+(?:un|une)\s+"
            r"(?:rappel|alarme|alerte)\b",
        ),
        ("en", r"\bremind\s+(?:me|us)\b"),
        (
            "en",
            r"\b(?:set|create|add|make|schedule|put|setup)\s+(?:me\s+|up\s+)?"
            r"(?:a|an)\s+(?:reminder|alarm|alert)\b",
        ),
        ("de", r"\berinner(?:e|n|t)?\s+(?:mich|uns)\b"),
        ("de", r"\berinnern\s+sie\s+(?:mich|uns)\b"),
        (
            "de",
            r"\b(?:du|sie|ihr)\b[^.?!]{0,20}?\b(?:mich|uns)\b"
            r"(?=[^.?!]{0,60}\berinnern\b)",
        ),
        (
            "de",
            r"\b(?:stell|stelle|erstell|erstelle|setz|setze|mach|mache|leg|lege)\s+"
            r"(?:mir\s+|uns\s+)?(?:eine?n?)\s+(?:erinnerung|wecker|alarm)\b",
        ),
        ("es", r"\brecuerd[ae](?:me|nos)\b"),
        ("es", r"\brecordar(?:me|nos)\b"),
        ("es", r"\b(?:me|nos)\s+(?:recuerdas|recuerdes|recordaras|recordarias)\b"),
        (
            "es",
            r"\b(?:pon|ponme|crea|creame|programa|programame|anade|anademe|agrega|"
            r"hazme|haz)\s+(?:un|una)\s+(?:recordatorio|alarma|aviso)\b",
        ),
        ("it", r"\bricord[ai](?:mi|ci)\b"),
        ("it", r"\bricordar(?:mi|ci)\b"),
        ("it", r"\b(?:mi|ci)\s+ricord(?:i|eresti)\b"),
        (
            "it",
            r"\b(?:imposta|impostami|crea|creami|metti|mettimi|aggiungi|programma|"
            r"fammi)\s+(?:un|una|un')\s*+(?:promemoria|sveglia|allarme|avviso)\b",
        ),
    )
)

# A complement that asks for information: "remind me what X means".
QUESTION_START = {
    "fr": re.compile(
        r"(?:ce\s+qu|comment|qui|pourquoi|quel|quelle|quels|quelles|ou|combien|"
        r"quand|si|la\s+definition|le\s+sens|la\s+signification)\b"
    ),
    "en": re.compile(r"(?:what|how|who|whom|whose|why|which|where|when|whether|if)\b"),
    "de": re.compile(r"(?:was|wie|wer|wem|wen|warum|welche\w*|wo|wann|ob)\b"),
    # Spanish and Italian read the accents: "que" asks only with its accent.
    "es": re.compile(
        r"(?:qué|cómo|quién|quiénes|cuál|cuáles|dónde|cuándo|cuánto|cuánta|"
        r"por\s+qué|lo\s+que|si)\b"
    ),
    "it": re.compile(
        r"(?:cosa|che\s+cosa|come|chi|quale|quali|dove|quando|quanto|perch[eé]|se)\b"
    ),
}
# A complement that names something to do: "remind me to call", "d'appeler".
ACTION_START = {
    "fr": re.compile(
        r"(?:de\b|d'|qu'|que\s+(?:je|j'|tu|il|elle|on|nous|vous|ils|elles|mon|ma|"
        r"mes|le|la|les)\b|pour\b)"
    ),
    "en": re.compile(r"(?:to|about|that|of)\b"),
    "de": re.compile(r"(?:an|daran|dass)\b"),
    "es": re.compile(r"(?:que|de)\b|\w+(?:ar|er|ir)(?:me|le|lo|la|les|los|las|se)?\b"),
    "it": re.compile(r"(?:di|che)\b|\w+(?:are|ere|ire)\b"),
}

# ------------------------------------------------------------- expressions

LEAD_EVERY = (
    r"(?:tous\s+les|toutes\s+les|chaque|every|each|jeden|jede|jedes|alle|"
    r"todos\s+los|todas\s+las|cada|ogni|tutti\s+i|tutte\s+le|tutti\s+gli)"
)
DURATION = re.compile(
    rf"(?:(?P<n1>{QTY})\s*+(?P<u1>{ANY_UNIT})(?:\b|(?=\d))"
    rf"(?:\s*+(?:et\s+|and\s+|und\s+|y\s+|e\s+)?(?P<n2>\d{{1,2}})\b(?:\s*+{MINUTE_UNIT}\b)?)?"
    r"(?P<plus>\s+(?:et\s+demie|and\s+a\s+half|y\s+media|e\s+mezza|und\s+eine\s+halbe)\b)?"
    r"|(?P<half>une\s+demi[\s-]heure|demi[\s-]heure|half\s+an\s+hour|a\s+half\s+hour|"
    r"einer\s+halben\s+stunde|eine\s+halbe\s+stunde|media\s+hora|mezz'?\s?ora|mezza\s+ora)\b"
    r"|(?P<quarter>un\s+quart\s+d'heure|quart\s+d'heure|a\s+quarter\s+of\s+an\s+hour|"
    r"a\s+quarter\s+hour|einer\s+viertelstunde|eine\s+viertelstunde|"
    r"un\s+cuarto\s+de\s+hora|un\s+quarto\s+d'ora)\b)"
)
RELATIVE_LEAD = re.compile(r"\b(?:dans|d'ici|in|en|dentro\s+de|tra|fra|entro)\s+$")
EARLY_AFTER = re.compile(
    r"\s+(?:avant|plus\s+tot|before(?:hand)?|in\s+advance|earlier|early|vorher|"
    r"davor|zuvor|fruher|antes|prima|in\s+anticipo|d'avance|a\s+l'avance|"
    r"de\s+antelacion|de\s+antemano)\b"
)
EARLY_LEAD = re.compile(
    r"\b(?:avec\s+)?(?:une?\s+|an?\s+|eine?n?\s+|un\s+)?"
    r"(?:alerte|alarme|alert|alarm|aviso|avviso|rappel|reminder)\s+$"
)
DAY_BEFORE = re.compile(
    r"\b(?:la\s+veille|the\s+day\s+before|the\s+night\s+before|am\s+vortag|"
    r"am\s+tag\s+davor|el\s+dia\s+antes|la\s+vispera|il\s+giorno\s+prima)\b"
)
EVERY_OTHER = re.compile(
    rf"\b(?:{LEAD_EVERY}\s+(?P<n>{QTY})|every\s+other|jeden\s+zweiten)\s+"
    rf"(?P<u>{ANY_UNIT})\b"
)
WORKDAYS = re.compile(
    r"\b(?:en\s+semaine|les\s+jours\s+de\s+semaine|les\s+jours\s+ouvres|"
    r"du\s+lundi\s+au\s+vendredi|every\s+weekday|on\s+weekdays|weekdays|werktags|"
    r"an\s+werktagen|montags\s+bis\s+freitags|von\s+montag\s+bis\s+freitag|"
    r"de\s+lunes\s+a\s+viernes|entre\s+semana|dal\s+lunedi\s+al\s+venerdi|"
    r"nei\s+giorni\s+feriali|ogni\s+giorno\s+feriale)\b"
)
WEEKLY_LEAD = re.compile(rf"\b(?:{LEAD_EVERY}|les|los|on)\s+(?={WD})")
WEEKDAY_WORD = re.compile(rf"(?P<wd>{WD})(?P<s>s)?\b")
JOINT = re.compile(r"\s*+(?:,|et|and|und|y|e)\s*+")
WEEKDAY_ADVERB = re.compile(
    r"\b(?:montags|dienstags|mittwochs|donnerstags|freitags|samstags|sonntags)\b"
)
DAILY = re.compile(
    rf"\b{LEAD_EVERY}\s+(?:jours?|days?|tage?|dias?|giorni|giorno)\b"
    r"|\b(?:quotidiennement|daily|everyday|taglich|taeglich|diariamente|quotidianamente)\b"
)
DAILY_PART = re.compile(
    rf"\b{LEAD_EVERY}\s+(?P<part>matins?|soirs?|mornings?|evenings?|nights?|"
    r"morgen|abend|mananas?|tardes?|noches?|mattin[ae]|ser[ae])\b"
)
WEEKLY = re.compile(
    rf"\b{LEAD_EVERY}\s+(?:semaines?|weeks?|wochen?|semanas?|settimane|settimana)\b"
    r"|\b(?:hebdomadairement|weekly|wochentlich|semanalmente|settimanalmente)\b"
)
MONTHLY = re.compile(
    rf"\b(?:(?:de|of|di|des)\s+)?{LEAD_EVERY}\s+(?:mois|months?|monat|monate|meses|"
    r"mes|mesi|mese)\b|\b(?:jedes\s+monats|mensuellement|monthly|monatlich|"
    r"mensualmente|mensilmente)\b"
)
YEARLY = re.compile(
    rf"\b{LEAD_EVERY}\s+(?:ans|annees|years?|jahre?s?|anos|anni|anno)\b"
    r"|\b(?:annuellement|yearly|annually|jahrlich|anualmente|annualmente)\b"
)
MONTH_DAY = re.compile(
    r"\b(?:le|the|on\s+the|on|am|el\s+dia|el|il)\s+(?P<d>\d{1,2})"
    r"(?:er|st|nd|rd|th|\.)?(?![\d:h])"
)
ISO_DATE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")
NAMED_DATE = re.compile(
    r"\b(?:(?:le|the|on\s+the|on|am|el|il|del|dal|ce)\s+)?"
    rf"(?:(?P<wd>{WD}),?\s+(?:(?:le|the|den|el|il)\s+)?)?"
    r"(?P<d>\d{1,2}|premier)(?:er|st|nd|rd|th|\.|º)?\s+(?:de\s+|of\s+)?"
    rf"(?P<mn>{MONTH})\b\.?(?:\s+(?:de\s+)?(?P<y>\d{{4}}))?"
)
NAMED_DATE_EN = re.compile(
    rf"\b(?:on\s+)?(?:(?P<wd>{WD}),?\s+)?(?P<mn>{MONTH})\.?\s+(?P<d>\d{{1,2}})"
    r"(?:st|nd|rd|th)?\b(?:,?\s+(?P<y>\d{4}))?"
)
NUMERIC_DATE = re.compile(
    r"\b(?:(?:le|the|on|am|el|il)\s+)?(?P<a>\d{1,2})(?:/(?P<b>\d{1,2})(?:/(?P<y>\d{2,4}))?"
    r"|\.(?P<b2>\d{1,2})\.(?P<y2>\d{2,4})?)(?![\d:])"
)
AFTER_TOMORROW = re.compile(
    r"\b(?:apres[\s-]demain|(?:the\s+)?day\s+after\s+tomorrow|ubermorgen|uebermorgen|"
    r"pasado\s+manana|dopodomani)\b"
)
TODAY_PART = re.compile(
    r"\b(?:ce\s+(?P<a>matin|soir)|cet\s+(?P<b>apres[\s-]midi)|cette\s+(?P<c>nuit)|"
    r"this\s+(?P<d>morning|afternoon|evening)|"
    r"heute\s+(?P<e>morgen|fruh|vormittag|mittag|nachmittag|abend|nacht)|"
    r"esta\s+(?P<f>manana|tarde|noche)|questa\s+(?P<g>mattina|sera|notte)|"
    r"questo\s+(?P<h>pomeriggio)|(?P<i>stasera|stamattina|stamani|stanotte|tonight))\b"
)
TODAY = re.compile(r"\b(?:aujourd'?\s?hui|aujourdhui|today|heute|hoy|oggi)\b")
TOMORROW = re.compile(r"\b(?:demain|tomorrow|tmrw|tmr|morgen|manana|domani)\b")
MORGEN_PART = re.compile(r"\s+(fruh|vormittag|mittag|nachmittag|abend|nacht)\b")
NEXT = r"(?:prochaine?|next|nachsten|naechsten|kommenden|proxima|proximo|prossima|prossimo)"
WEEKDAY_EXPR = re.compile(
    r"\b(?:(?:le|ce|on|this|am|diesen|el|este|il|questo|au)\s+)?"
    rf"(?:(?P<before>{NEXT})\s+)?(?P<wd>{WD})(?P<plural>s)?\b"
    rf"(?:\s+(?P<after>{NEXT}|que\s+viene))?"
)
CLOCK_PREFIXED = re.compile(
    rf"\b(?:(?:at|um|a|vers)\s+)?(?P<kind>half\s+past|quarter\s+past|quarter\s+to|"
    rf"halb|viertel\s+nach|viertel\s+vor|dreiviertel)\s+(?P<h>{HOURW})\b"
)
CLOCK_MERIDIEM = re.compile(
    r"\b(?:(?:at|for|around|by)\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*+"
    r"(?P<ap>[ap])\.?\s?m\b\.?"
)
CLOCK_COLON = re.compile(
    r"\b(?:(?:a|at|um|gegen|vers|alle|a\s+las|a\s+la|for|around|by|verso)\s+)?"
    r"(?P<h>\d{1,2}):(?P<m>\d{2})(?:\s*+uhr\b)?"
)
CLOCK_DOT = re.compile(
    r"\b(?:(?:um|at|alle|a\s+las|a\s+la|gegen)\s+(?P<h>\d{1,2})\.(?P<m>\d{2})\b"
    r"(?:\s*+uhr\b)?|(?P<h2>\d{1,2})\.(?P<m2>\d{2})\s*+uhr\b)"
)
CLOCK_UNIT = re.compile(
    rf"\b(?:(?:a|vers|pour|um|gegen|at|alle|a\s+las|a\s+la)\s+)?(?P<h>{HOURW})\s*+"
    r"(?P<u>h|heures?|hrs?|uhr|o'?clock)(?:\b|(?=\d))(?:\s*+(?P<m>\d{2})\b)?"
)
CLOCK_NOON = re.compile(
    r"\b(?:(?:a|at|um|al|alle)\s+)?(?P<w>midi|noon|midday|mittags?|mediodia|"
    r"mezzogiorno|minuit|midnight|mitternacht|medianoche|mezzanotte)\b"
)
CLOCK_BARE = re.compile(
    r"\b(?:at|a|um|gegen|alle|a\s+las|a\s+la|vers|verso|por\s+las|around|for)\s+"
    r"(?P<h>\d{1,2})\b"
)
CLOCK_WORD = re.compile(
    rf"\b(?:at|um|gegen|alle|a\s+las|a\s+la|around)\s+(?P<h>{_words(HOUR_WORDS)})\b"
)
NOT_A_TIME = re.compile(
    r"(?:er|eme|e|st|nd|rd|th)\b|"
    rf"\s*+(?:{ANY_UNIT}\b|[/.,:]\d|%|euros?\b|dollars?\b|eur\b|usd\b|€|\$|"
    r"personnes?\b|people\b|personen\b|personas\b|persone\b|ans\b|years?\b)"
)
MINUTE_TAIL = re.compile(
    r"\s*+(?:(?P<half>et\s+demie?|and\s+a\s+half|y\s+media|e\s+mezz[ao]|e\s+trenta)"
    r"|(?P<quarter>et\s+quart|y\s+cuarto|e\s+un\s+quarto|e\s+quarto)"
    r"|(?P<less>moins\s+(?:le\s+)?quart|menos\s+cuarto|meno\s+(?:un\s+)?quarto))\b"
)
MERIDIEM_TAIL = re.compile(r"\s*+(?P<ap>[ap])\.?\s?m\b\.?")
DAYPART = re.compile(
    r"\b(?:(?:du|le|ce|in\s+the|this|am|de\s+la|por\s+la|esta|di|la|questa|al|"
    r"en\s+la|nel|del|de|in)\s+)?(?P<part>matinee|matin|morning|morgens|vormittags?|"
    r"fruh|manana|mattina|mattino|apres[\s-]midi|afternoon|nachmittags?|tarde|"
    r"pomeriggio|soiree|soir|evening|abends?|noche|sera|nuit|night|nachts?|notte)\b"
)
MAS_TARDE = re.compile(r"\bmas\s+$")
MORGEN_NOT_TOMORROW = re.compile(r"\b(?:heute|am|jeden|guten|gestern)\s+$")
MANANA_NOT_TOMORROW = re.compile(
    r"\b(?:la|de\s+la|por\s+la|esta|cada|las|todas\s+las|en\s+la)\s+$"
)
LEADING = re.compile(
    r"(?:[\s,:;.!\-–—]|de\b|d'|du\b|des\b|to\b|that\b|about\b|of\b|for\b|zu\b|an\b|"
    r"dass\b|daran\b|que\b|qu'|di\b|pour\b|por\b|para\b|per\b|et\b|and\b|und\b|"
    r"y\b|e\b|please\b|pls\b|stp\b|svp\b|bitte\b|merci\b|s'il\s+te\s+plait\b|"
    r"s'il\s+vous\s+plait\b|por\s+favor\b|per\s+favore\b|me\b|mir\b|mich\b|mi\b|"
    r"a\b|au\b|aux\b|at\b|on\b|le\b|la\b|les\b|el\b|il\b|lo\b|am\b|um\b|"
    r"erinnern\b|un\s+rappel\b|a\s+reminder\b|eine\s+erinnerung\b|"
    r"un\s+recordatorio\b|un\s+promemoria\b|rappel\b|reminder\b)"
)
TRAILING = re.compile(
    r"(?:[\s,:;.!?\-–—]|\bplease|\bpls|\bstp|\bsvp|\bbitte|\bmerci|\bthanks|"
    r"\bthank\s+you|\bdanke|\bgracias|\bgrazie|\bpor\s+favor|\bper\s+favore|"
    r"\bs'il\s+te\s+plait|\bs'il\s+vous\s+plait|\bet|\band|\bund|\bde|\bto|\bat|"
    r"\bpour|\bfor|\bam|\bum|\ba|\bdi|\bel|\ble|\bla|\berinnern)$"
)
GERMAN_ARTICLE = re.compile(r"(?:den|die|das|dem|meinen|meine|meinem)\s+")
SENTENCE_END = re.compile(r"[.!?]+\s+(?=\S)|\n+")
POLITE_BEFORE = re.compile(
    r"(?:\W|salut|bonjour|hello|hi|hey|hallo|hola|ciao|atlas|stp|svp|please|bitte|"
    r"por\s+favor|per\s+favore|peux[\s-]tu|tu\s+peux|pourrais[\s-]tu|pouvez[\s-]vous|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|kannst\s+du|konntest\s+du|"
    r"konnen\s+sie|puedes|podrias|puede|puoi|potresti|puo|oh|okay|ok)*+"
)
TOKEN = re.compile(r"[a-z]+")


# ------------------------------------------------------------------- models


@dataclass(frozen=True)
class Clock:
    hour: int
    minute: int
    meridiem: str | None = None  # "am" | "pm"
    explicit: bool = False  # 24-hour reading ("16 h", "18:30", "9 Uhr")
    midnight: bool = False


Day = tuple[str, int, int, int]  # ("offset"|"months"|"weekday"|"date"|"ambiguous", ...)


@dataclass
class Spec:
    """What the person wrote, before any calendar arithmetic."""

    language: str
    day: Day | None = None
    clock: Clock | None = None
    daypart: str | None = None
    delta: timedelta | None = None
    recurrence: Recurrence | None = None
    early: timedelta | None = None
    title: str = ""
    extra: bool = False
    asks_information: bool = False
    asks_action: bool = False

    @property
    def has_time_information(self) -> bool:
        return any(
            value is not None
            for value in (
                self.day,
                self.clock,
                self.daypart,
                self.delta,
                self.recurrence,
            )
        )

    @property
    def is_request(self) -> bool:
        """An explicit request to be reminded, not a question about something."""
        if self.asks_information:
            return False
        return self.has_time_information or self.asks_action

    def merged(self, later: Spec) -> Spec:
        """A follow-up answer completes the request: its fields win."""
        out = replace(self)
        if later.delta is not None:
            out.day, out.clock, out.daypart = None, None, None
        for name in ("day", "clock", "daypart", "delta", "recurrence", "early"):
            value = getattr(later, name)
            if value is not None:
                setattr(out, name, value)
        return out


@dataclass(frozen=True)
class Plan:
    """A reminder ready to be written: first occurrence, zone and options."""

    title: str
    start: datetime
    zone: str
    recurrence: Recurrence | None
    early: timedelta | None
    language: str


@dataclass(frozen=True)
class Question:
    """What is missing: "when" (nothing), "time", "past" or "date"."""

    kind: str
    language: str


# ------------------------------------------------------------ text mapping


class Text:
    """Folded text with typos corrected and a map back to the original string."""

    def __init__(self, original: str, language: str | None = None) -> None:
        self.original = original
        folded = fold(original)
        vocabulary = TRIGGER_WORDS + (VOCABULARY[language] if language else ())
        pieces: list[str] = []
        starts: list[int] = []
        ends: list[int] = []
        position = 0
        for token in TOKEN.finditer(folded):
            for index in range(position, token.start()):
                pieces.append(folded[index])
                starts.append(index)
                ends.append(index + 1)
            replacement = _corrected(token.group(), vocabulary)
            s, e = token.start(), token.end()
            for k, char in enumerate(replacement):
                pieces.append(char)
                starts.append(s if k == 0 else min(s + k, e - 1))
                ends.append(e if k == len(replacement) - 1 else min(s + k + 1, e))
            position = e
        for index in range(position, len(folded)):
            pieces.append(folded[index])
            starts.append(index)
            ends.append(index + 1)
        self.value = "".join(pieces)
        self.starts, self.ends = starts, ends
        self.used: list[tuple[int, int]] = []

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        if end <= start:
            index = (
                self.starts[start] if start < len(self.starts) else len(self.original)
            )
            return index, index
        return self.starts[start], self.ends[end - 1]

    def free(self, start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b in self.used)

    def take(self, start: int, end: int) -> None:
        self.used.append((start, end))

    def matches(self, pattern: re.Pattern[str]) -> list[re.Match[str]]:
        return [
            m
            for m in pattern.finditer(self.value)
            if m.end() > m.start() and self.free(m.start(), m.end())
        ]


def trigger_language(text: str) -> str | None:
    """The language of an explicit reminder request, or None."""
    if len(text) > MAX_TEXT:
        return None
    mapped = Text(text)
    for language, pattern in TRIGGERS:
        if pattern.search(mapped.value):
            return language
    return None


# ------------------------------------------------------------------ parsing


def _number(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return NUMBERS.get(word)


def _unit(word: str) -> str:
    for name, pattern in UNITS.items():
        if re.fullmatch(pattern, word):
            return name
    return "minutes"


def _duration(found: re.Match[str]) -> tuple[str, int, int] | None:
    """(unit, amount, end) of a DURATION match; hours and minutes become minutes."""
    if found.group("half"):
        return "minutes", 30, found.end()
    if found.group("quarter"):
        return "minutes", 15, found.end()
    amount = _number(found.group("n1") or "")
    if amount is None:
        return None
    unit = _unit(found.group("u1"))
    if unit == "hours":
        minutes = amount * 60 + int(found.group("n2") or 0)
        if found.group("plus"):
            minutes += 30
        return "minutes", minutes, found.end()
    if unit == "minutes":
        return "minutes", amount, found.end("u1")
    return unit, amount, found.end("u1")


def _hour(word: str) -> int | None:
    value = _number(word)
    return value if value is not None and 0 <= value <= 24 else None


def parse(text: str, language: str | None = None) -> Spec | None:
    """Read a reminder request; None when it is not one.

    With a language and no trigger, the text is read as the answer to a
    question asked about an earlier request ("à 18 h", "tomorrow at 9").
    """
    if len(text) > MAX_TEXT:
        return None
    found_language = trigger_language(text)
    if found_language is None:
        return None if language is None else _read(Text(text, language), language, None)
    mapped = Text(text, found_language)
    span = next(
        (
            (m.start(), m.end())
            for lang, pattern in TRIGGERS
            if lang == found_language
            for m in [pattern.search(mapped.value)]
            if m
        ),
        None,
    )
    if span is None:
        return None
    return _read(mapped, found_language, span)


def _read(mapped: Text, language: str, trigger: tuple[int, int] | None) -> Spec:
    t = mapped.value
    spec = Spec(language)
    if trigger:
        mapped.take(*trigger)
        _complement(mapped, spec, trigger)
    for m in mapped.matches(DAY_BEFORE):
        spec.early = timedelta(days=1)
        lead = EARLY_LEAD.search(t[: m.start()])
        mapped.take(lead.start() if lead else m.start(), m.end())
        break
    for m in mapped.matches(DURATION):
        amount = _duration(m)
        tail = EARLY_AFTER.match(t, amount[2]) if amount else None
        if amount is None or tail is None or spec.early is not None:
            continue
        unit, value, _ = amount
        spec.early = (
            timedelta(days=value)
            if unit == "days"
            else timedelta(weeks=value)
            if unit == "weeks"
            else timedelta(minutes=value)
        )
        lead = EARLY_LEAD.search(t[: m.start()])
        mapped.take(lead.start() if lead else m.start(), tail.end())
    _recurrence(mapped, spec)
    for m in mapped.matches(DURATION):
        lead = RELATIVE_LEAD.search(t[: m.start()])
        amount = _duration(m)
        if not lead or amount is None or not mapped.free(lead.start(), m.start()):
            continue
        unit, value, end = amount
        if unit == "minutes":
            spec.delta = timedelta(minutes=value)
        elif unit == "months":
            spec.day = ("months", value, 0, 0)
        else:
            spec.day = ("offset", value * (7 if unit == "weeks" else 1), 0, 0)
        mapped.take(lead.start(), end)
        break
    _dates(mapped, spec, language)
    _clock(mapped, spec, language)
    for m in mapped.matches(DAYPART):
        word = m.group("part").replace("-", " ")
        if PART_LANGUAGE.get(word, language) != language:
            continue
        if word == "tarde" and MAS_TARDE.search(t[: m.start()]):
            continue
        if spec.daypart is None and word in PART_OF:
            spec.daypart = PART_OF[word]
            mapped.take(m.start(), m.end())
    if trigger:
        _title(mapped, spec, trigger)
    return spec


def _complement(mapped: Text, spec: Spec, trigger: tuple[int, int]) -> None:
    """Does the text right after the trigger ask something, or name a task?"""
    end = mapped.original_span(*trigger)[1]
    rest = re.sub(r"^[\s,:;-]+", "", mapped.original[end:]).lower()
    folded = fold(rest)
    language = spec.language
    if rest and QUESTION_START[language].match(
        rest if language in {"es", "it"} else folded
    ):
        spec.asks_information = True
        return
    spec.asks_action = bool(
        rest
        and (
            ACTION_START[language].match(folded)
            or (language == "de" and _zu_infinitive(folded))
        )
    )


def _zu_infinitive(folded: str) -> bool:
    """German "..., rauszubringen": an infinitive with zu names a task."""
    words = TOKEN.findall(folded)
    return any(word == "zu" for word in words[:-1]) or any(
        len(word) > 6 and "zu" in word[2:-2] and word.endswith("en") for word in words
    )


def _recurrence(mapped: Text, spec: Spec) -> None:
    t = mapped.value
    frequencies = {
        "hours": "HOURLY",
        "days": "DAILY",
        "weeks": "WEEKLY",
        "months": "MONTHLY",
    }
    for m in mapped.matches(EVERY_OTHER):
        unit = _unit(m.group("u"))
        amount = 2 if m.group("n") is None else _number(m.group("n"))
        if not amount or amount > 366 or unit not in frequencies:
            continue
        spec.recurrence = Recurrence(frequencies[unit], amount)
        mapped.take(m.start(), m.end())
        return
    for m in mapped.matches(WORKDAYS):
        spec.recurrence = Recurrence("WEEKLY", byday=(0, 1, 2, 3, 4))
        mapped.take(m.start(), m.end())
        return
    for m in mapped.matches(WEEKLY_LEAD):
        lead = m.group().strip()
        days: list[int] = []
        position = end = m.end()
        while day := WEEKDAY_WORD.match(t, position):
            name = day.group("wd")
            plural = bool(day.group("s")) or name.endswith("s")
            if lead in {"les", "on"} and not plural:
                break
            days.append(WEEKDAYS[name])
            end = day.end()
            joint = JOINT.match(t, end)
            if not joint or not WEEKDAY_WORD.match(t, joint.end()):
                break
            position = joint.end()
        if days:
            spec.recurrence = Recurrence("WEEKLY", byday=tuple(sorted(set(days))))
            mapped.take(m.start(), end)
            return
    adverbs = mapped.matches(WEEKDAY_ADVERB)
    if adverbs:
        for a in adverbs:
            mapped.take(a.start(), a.end())
        days = sorted({WEEKDAYS[a.group()[:-1]] for a in adverbs})
        spec.recurrence = Recurrence("WEEKLY", byday=tuple(days))
        return
    for m in mapped.matches(DAILY_PART):
        spec.recurrence = Recurrence("DAILY")
        spec.daypart = PART_OF.get(m.group("part"))
        mapped.take(m.start(), m.end())
        return
    for pattern, freq in ((DAILY, "DAILY"), (WEEKLY, "WEEKLY")):
        for m in mapped.matches(pattern):
            spec.recurrence = Recurrence(freq)
            mapped.take(m.start(), m.end())
            return
    for m in mapped.matches(MONTHLY):
        mapped.take(m.start(), m.end())
        monthday = None
        for d in mapped.matches(MONTH_DAY):
            if 1 <= int(d.group("d")) <= 31:
                monthday = int(d.group("d"))
                mapped.take(d.start(), d.end())
                break
        spec.recurrence = Recurrence("MONTHLY", bymonthday=monthday)
        return
    for m in mapped.matches(YEARLY):
        spec.recurrence = Recurrence("YEARLY")
        mapped.take(m.start(), m.end())
        return


def _dates(mapped: Text, spec: Spec, language: str) -> None:
    t = mapped.value
    for m in mapped.matches(ISO_DATE):
        spec.day = ("date", int(m.group("y")), int(m.group("m")), int(m.group("d")))
        mapped.take(m.start(), m.end())
        break
    for pattern in (NAMED_DATE, NAMED_DATE_EN):
        for m in mapped.matches(pattern) if spec.day is None else []:
            raw = m.group("d")
            day = 1 if raw == "premier" else int(raw)
            year = int(m.group("y")) if m.group("y") else 0
            spec.day = ("date", year, MONTHS[m.group("mn")], day)
            mapped.take(m.start(), m.end())
            break
    for m in mapped.matches(NUMERIC_DATE) if spec.day is None else []:
        first = int(m.group("a"))
        second = int(m.group("b") or m.group("b2") or 0)
        raw_year = m.group("y") or m.group("y2")
        year = int(raw_year) if raw_year else 0
        if 0 < year < 100:
            year += 2000
        if language == "en" and first <= 12 and second <= 12 and first != second:
            spec.day = ("ambiguous", 0, 0, 0)
        elif language == "en" and first <= 12 < second <= 31:
            spec.day = ("date", year, first, second)
        elif 1 <= second <= 12 and 1 <= first <= 31:
            spec.day = ("date", year, second, first)
        else:
            continue
        mapped.take(m.start(), m.end())
        break
    for m in mapped.matches(AFTER_TOMORROW):
        if spec.day is None:
            spec.day = ("offset", 2, 0, 0)
        mapped.take(m.start(), m.end())
        break
    for m in mapped.matches(TODAY_PART):
        part = next((g for g in m.groups() if g), None) or "tonight"
        if spec.day is None:
            spec.day = ("offset", 0, 0, 0)
        spec.daypart = PART_OF.get(part.replace("-", " ")) or spec.daypart
        if part == "mittag" and spec.clock is None:
            spec.clock = Clock(12, 0, explicit=True)
        mapped.take(m.start(), m.end())
        break
    for m in mapped.matches(TODAY):
        if spec.day is None:
            spec.day = ("offset", 0, 0, 0)
        mapped.take(m.start(), m.end())
        break
    for m in mapped.matches(TOMORROW):
        word, before = m.group(), t[: m.start()]
        if word == "morgen" and MORGEN_NOT_TOMORROW.search(before):
            continue
        if word == "manana" and MANANA_NOT_TOMORROW.search(before):
            continue
        if spec.day is None:
            spec.day = ("offset", 1, 0, 0)
        mapped.take(m.start(), m.end())
        follow = MORGEN_PART.match(t, m.end()) if word == "morgen" else None
        if follow:
            spec.daypart = PART_OF.get(follow.group(1), "morning")
            if follow.group(1) == "mittag":
                spec.clock = Clock(12, 0, explicit=True)
            mapped.take(follow.start(), follow.end())
        break
    for m in mapped.matches(WEEKDAY_EXPR):
        name = m.group("wd")
        if m.group("plural") and spec.recurrence is None:
            continue
        following = bool(m.group("before") or m.group("after"))
        if spec.day is None:
            spec.day = ("weekday", WEEKDAYS[name], int(following), 0)
        mapped.take(m.start(), m.end())
        break


def _clock(mapped: Text, spec: Spec, language: str) -> None:
    t = mapped.value
    found = _clock_candidate(mapped, language)
    if found is None:
        return
    clock, start, end = found
    tail = MINUTE_TAIL.match(t, end)
    if tail and clock.minute == 0 and not clock.midnight:
        if tail.group("half"):
            clock = replace(clock, minute=30)
        elif tail.group("quarter"):
            clock = replace(clock, minute=15)
        else:
            clock = replace(clock, hour=(clock.hour - 1) % 24, minute=45)
        end = tail.end()
    meridiem = MERIDIEM_TAIL.match(t, end)
    if meridiem and clock.meridiem is None and 1 <= clock.hour <= 12:
        clock = replace(clock, meridiem="am" if meridiem.group("ap") == "a" else "pm")
        end = meridiem.end()
    spec.clock = clock
    mapped.take(start, end)


def _clock_candidate(mapped: Text, language: str) -> tuple[Clock, int, int] | None:
    t = mapped.value
    for m in mapped.matches(CLOCK_PREFIXED):
        hour = _hour(m.group("h"))
        if hour is None:
            continue
        kind = m.group("kind")
        minute = {"half past": 30, "quarter past": 15, "viertel nach": 15}.get(kind)
        if minute is not None:
            return Clock(hour, minute), m.start(), m.end()
        minute = 30 if kind == "halb" else 45
        return Clock((hour - 1) % 24, minute), m.start(), m.end()
    for m in mapped.matches(CLOCK_MERIDIEM):
        hour = int(m.group("h"))
        if 1 <= hour <= 12:
            meridiem = "am" if m.group("ap") == "a" else "pm"
            return Clock(hour, int(m.group("m") or 0), meridiem), m.start(), m.end()
    for pattern in (CLOCK_COLON, CLOCK_DOT, CLOCK_UNIT):
        for m in mapped.matches(pattern):
            groups = m.groupdict()
            raw_hour = groups.get("h") or groups.get("h2") or ""
            hour = _hour(raw_hour)
            minute = int(groups.get("m") or groups.get("m2") or 0)
            if hour is None or minute > 59:
                continue
            twelve = language == "en" or "clock" in (groups.get("u") or "")
            explicit = (
                not twelve or hour > 12 or hour in {0, 24} or raw_hour.startswith("0")
            )
            return Clock(hour % 24, minute, explicit=explicit), m.start(), m.end()
    for m in mapped.matches(CLOCK_NOON):
        midnight = m.group("w") in {
            "minuit", "midnight", "mitternacht", "medianoche", "mezzanotte",
        }  # fmt: skip
        return (
            Clock(0 if midnight else 12, 0, explicit=True, midnight=midnight),
            m.start(),
            m.end(),
        )
    for pattern in (CLOCK_BARE, CLOCK_WORD):
        for m in mapped.matches(pattern):
            if NOT_A_TIME.match(t, m.end()):
                continue
            hour = _hour(m.group("h"))
            if hour is None or hour > 23:
                continue
            explicit = (language != "en" and hour > 12) or hour == 0
            return Clock(hour, 0, explicit=explicit), m.start(), m.end()
    return None


def _title(mapped: Text, spec: Spec, trigger: tuple[int, int]) -> None:
    original = mapped.original
    keep = [True] * len(original)
    for a, b in (mapped.original_span(*span) for span in mapped.used):
        for index in range(a, min(b, len(original))):
            keep[index] = False

    def piece(a: int, b: int) -> str:
        chars = [c if keep[i] else " " for i, c in enumerate(original[a:b], start=a)]
        return re.sub(r"\s+", " ", "".join(chars)).strip()

    trigger_start, trigger_end = mapped.original_span(*trigger)
    after, before = piece(trigger_end, len(original)), piece(0, trigger_start)
    if not after and not POLITE_BEFORE.fullmatch(fold(before)):
        # The task may come before the trigger, with nothing after it.
        after = before
    sentences = [s for s in SENTENCE_END.split(after) if s.strip()]
    spec.extra = any(len(fold(s).strip(" .,;:!?")) > 2 for s in sentences[1:])
    spec.title = clean_title(sentences[0] if sentences else "", spec.language)


def clean_title(value: str, language: str) -> str:
    text = value.strip()
    for _ in range(16):
        folded = fold(text)
        lead = LEADING.match(folded)
        if lead and lead.end() > 0:
            text = text[lead.end() :]
            continue
        trail = TRAILING.search(folded)
        if trail and trail.end() > trail.start():
            text = text[: trail.start()]
            continue
        break
    if language == "de":
        article = GERMAN_ARTICLE.match(fold(text))
        if article and len(text) > article.end():
            text = text[article.end() :]
    text = re.sub(r"\s+", " ", text).strip(" \"'«»“”„")
    if len(text) > MAX_TITLE:
        text = text[:MAX_TITLE].rsplit(" ", 1)[0]
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------- resolution


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def wall_time(day: date, clock: time, zone: ZoneInfo) -> datetime:
    """Wall time in zone; a time skipped by a clock change moves forward."""
    aware = datetime.combine(day, clock).replace(tzinfo=zone)
    return datetime.fromtimestamp(aware.timestamp(), zone)


def _hours(clock: Clock, daypart: str | None, dated: bool, language: str) -> list[int]:
    """Candidate hours on the 24-hour clock, the most likely first."""
    hour = clock.hour
    if clock.meridiem == "am":
        return [0 if hour == 12 else hour]
    if clock.meridiem == "pm":
        return [hour if hour == 12 else hour + 12]
    if daypart in {"afternoon", "evening"} and hour < 12:
        return [hour + 12]
    if daypart == "night" and 7 <= hour < 12:
        return [hour + 12]
    if daypart == "morning" or clock.explicit or hour == 0 or hour >= 12:
        return [hour]
    if dated:
        # "tomorrow at 3" in English is in the afternoon; the French "3 h" is not.
        return [hour + 12 if language == "en" and 1 <= hour <= 6 else hour]
    # Today, no day named: the next of the two readings of a 12-hour clock.
    return [hour, hour + 12]


def _days(spec: Spec, today: date) -> tuple[list[date], bool] | None:
    """Candidate days, and whether the first was named exactly."""
    day = spec.day
    if day is None:
        return [today, today + timedelta(days=1)], False
    kind = day[0]
    if kind == "offset":
        return [today + timedelta(days=day[1])], True
    if kind == "months":
        return [_add_months(today, day[1])], True
    if kind == "weekday":
        ahead = (day[1] - today.weekday()) % 7
        if day[2] and ahead == 0:
            ahead = 7
        options = [today + timedelta(days=ahead)]
        return (options + [today + timedelta(days=7)] if ahead == 0 else options), False
    _, year, month, monthday = day
    try:
        named = date(year or today.year, month, monthday)
    except ValueError:
        return None
    if year:
        return [named], True
    try:
        return [named, named.replace(year=named.year + 1)], False
    except ValueError:
        return [named], False


def resolve(spec: Spec, now: datetime, zone_name: str) -> Plan | Question:
    zone = ZoneInfo(zone_name)
    local_now = now.astimezone(zone)
    today = local_now.date()
    language, title = spec.language, spec.title
    if spec.day is not None and spec.day[0] == "ambiguous":
        return Question("date", language)
    recurrence = spec.recurrence
    if spec.delta is not None and recurrence is None:
        start = (now + spec.delta).astimezone(zone).replace(second=0, microsecond=0)
        return Plan(title, start, zone_name, None, spec.early, language)
    if recurrence is not None and recurrence.freq == "HOURLY" and spec.clock is None:
        start = (local_now + timedelta(hours=recurrence.interval)).replace(
            second=0, microsecond=0
        )
        return Plan(title, start, zone_name, recurrence, spec.early, language)
    if spec.clock is None:
        nothing = spec.day is None and spec.daypart is None and recurrence is None
        return Question("when" if nothing else "time", language)
    clock = spec.clock
    days = _days(spec, today)
    if days is None:
        return Question("date", language)
    options, exact = days
    if clock.midnight:
        options = [d + timedelta(days=1) for d in options]
    hours = _hours(clock, spec.daypart, spec.day is not None, language)

    def at(day: date, hour: int) -> datetime:
        return wall_time(day, time(hour % 24, clock.minute), zone)

    if recurrence is not None:
        candidate = options[0]
        for _ in range(800):
            if at(candidate, hours[0]) > local_now and _occurs(
                recurrence, candidate, spec
            ):
                break
            candidate += timedelta(days=1)
        else:
            return Question("date", language)
        if recurrence.freq == "WEEKLY" and not recurrence.byday:
            recurrence = replace(recurrence, byday=(candidate.weekday(),))
        if recurrence.freq == "MONTHLY" and recurrence.bymonthday is None:
            recurrence = replace(recurrence, bymonthday=candidate.day)
        start = at(candidate, hours[0])
        return Plan(title, start, zone_name, recurrence, spec.early, language)
    for option in options:
        for hour in hours:
            moment = at(option, hour)
            if moment > local_now:
                return Plan(title, moment, zone_name, None, spec.early, language)
        if exact:
            break
    return Question("past", language)


def _occurs(recurrence: Recurrence, day: date, spec: Spec) -> bool:
    if recurrence.freq == "WEEKLY" and recurrence.byday:
        return day.weekday() in recurrence.byday
    if recurrence.freq == "MONTHLY" and recurrence.bymonthday is not None:
        return day.day == recurrence.bymonthday
    if recurrence.freq == "YEARLY" and spec.day is not None and spec.day[0] == "date":
        return (day.month, day.day) == (spec.day[2], spec.day[3])
    return True
