"""Reminders in the chat: a calendar file with an alarm, made by code.

"Rappelle-moi à 16 h 15 d'aller chercher les filles à l'école", "remind me
tomorrow at 9 to call the plumber", "erinnere mich Freitag um 18 Uhr ...":
the turn is answered here, without a model call. reminder_parse reads the date,
time, recurrence and early alarm in the person's time zone; reminder_ics writes
an RFC 5545 file whose DISPLAY alarm makes the phone's calendar ring; the file
is published in this request's user-file area like every generated file, and
the answer is one sentence that states the date and time, then the link.

Only explicit requests are routed here ("j'ai un rappel de vaccin" and "remind
me what X means" are not). A missing or unclear time ("ce soir", no time at
all, a moment already past) gets one short question; the person's next message
completes the request. Nothing leaves the host: every privacy level, strict
included, allows it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp

from services.orchestrator.datecalc import DEFAULT_ZONE
from services.orchestrator.reminder_ics import Recurrence, calendar_file
from services.orchestrator.reminder_parse import (
    Plan,
    Question,
    Spec,
    fold,
    parse,
    resolve,
)
from services.orchestrator.stream_client import sink_context

if TYPE_CHECKING:
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction

MAX_FILE_BYTES = 64 * 1024
DOWNLOAD_PREFIX = "/api/v1/terminals/atlas-files/files/serve/"

QUESTIONS = {
    "when": {
        "fr": "Pour quand voulez-vous ce rappel ? Indiquez le jour et l’heure.",
        "en": "When should I remind you? Give the day and the time.",
        "de": "Wann soll ich Sie erinnern? Nennen Sie Tag und Uhrzeit.",
        "es": "¿Para cuándo quiere el recordatorio? Indique el día y la hora.",
        "it": "Per quando vuole il promemoria? Indichi il giorno e l’ora.",
    },
    "time": {
        "fr": "À quelle heure voulez-vous ce rappel ?",
        "en": "At what time should I remind you?",
        "de": "Um wie viel Uhr soll ich Sie erinnern?",
        "es": "¿A qué hora quiere el recordatorio?",
        "it": "A che ora vuole il promemoria?",
    },
    "past": {
        "fr": "Ce moment est déjà passé. Pour quand voulez-vous ce rappel ?",
        "en": "That time has already passed. When should I remind you?",
        "de": "Dieser Zeitpunkt ist schon vorbei. Wann soll ich Sie erinnern?",
        "es": "Ese momento ya ha pasado. ¿Para cuándo quiere el recordatorio?",
        "it": "Quel momento è già passato. Per quando vuole il promemoria?",
    },
    "date": {
        "fr": "Pour quelle date ? Écrivez le mois en toutes lettres, par exemple « le 4 mars ».",
        "en": "Which date do you mean? Write the month as a word, for example “4 March”.",
        "de": "Welches Datum meinen Sie? Schreiben Sie den Monat aus, zum Beispiel „4. März“.",
        "es": "¿Qué fecha quiere decir? Escriba el mes con letras, por ejemplo «4 de marzo».",
        "it": "Quale data intende? Scriva il mese in lettere, per esempio «4 marzo».",
    },
}
QUESTION_TEXTS = frozenset(
    fold(text).strip() for texts in QUESTIONS.values() for text in texts.values()
)
DEFAULT_TITLE = {
    "fr": "Rappel",
    "en": "Reminder",
    "de": "Erinnerung",
    "es": "Recordatorio",
    "it": "Promemoria",
}
CONFIRMATION = {
    "fr": ("Rappel{title} prévu {when}{early} : ouvrez ce fichier pour "
           "l’ajouter à votre agenda."),
    "en": ("Reminder{title} set for {when}{early}: open this file to add it to "
           "your calendar."),
    "de": ("Erinnerung{title} für {when}{early}: Öffnen Sie diese Datei, um sie "
           "in Ihren Kalender zu übernehmen."),
    "es": ("Recordatorio{title} para {when}{early}: abra este archivo para "
           "añadirlo a su calendario."),
    "it": ("Promemoria{title} per {when}{early}: apra questo file per "
           "aggiungerlo al suo calendario."),
}  # fmt: skip
# The reminder text in the sentence, quoted as each language quotes.
QUOTED = {
    "fr": " « {} »",
    "en": " “{}”",
    "de": " „{}“",
    "es": " «{}»",
    "it": " «{}»",
}
EXTRA = {
    "fr": "Je n’ai traité que le rappel : posez votre autre demande dans un nouveau message.",
    "en": "I only handled the reminder: ask your other question in a new message.",
    "de": "Ich habe nur die Erinnerung angelegt: Stellen Sie Ihre andere Frage in einer neuen Nachricht.",
    "es": "Solo he creado el recordatorio: haga su otra pregunta en un nuevo mensaje.",
    "it": "Ho creato solo il promemoria: faccia l’altra domanda in un nuovo messaggio.",
}
FAILURE = {
    "fr": "Le fichier du rappel n’a pas pu être enregistré : aucun rappel n’a été créé. Réessayez dans un instant.",
    "en": "The reminder file could not be saved, so no reminder was created. Please try again in a moment.",
    "de": "Die Datei der Erinnerung konnte nicht gespeichert werden; es wurde keine Erinnerung angelegt. Versuchen Sie es gleich noch einmal.",
    "es": "No se pudo guardar el archivo del recordatorio; no se ha creado ningún recordatorio. Inténtelo de nuevo en un momento.",
    "it": "Non è stato possibile salvare il file del promemoria; nessun promemoria è stato creato. Riprovi tra un momento.",
}
DATE_FORMAT = {
    "fr": "EEEE d MMMM",
    "en": "EEEE d MMMM",
    "de": "EEEE, d. MMMM",
    "es": "EEEE d 'de' MMMM",
    "it": "EEEE d MMMM",
}
YEAR_SUFFIX = {"fr": " y", "en": " y", "de": " y", "es": " 'de' y", "it": " y"}
MONTH_DAY_FORMAT = {
    "fr": "d MMMM",
    "en": "d MMMM",
    "de": "d. MMMM",
    "es": "d 'de' MMMM",
    "it": "d MMMM",
}
RELATIVE_DAYS = {
    "fr": ("aujourd’hui", "demain"),
    "en": ("today", "tomorrow"),
    "de": ("heute", "morgen"),
    "es": ("hoy", "mañana"),
    "it": ("oggi", "domani"),
}
UNIT_NAMES = {
    "fr": {
        "minutes": ("minute", "minutes"),
        "hours": ("heure", "heures"),
        "days": ("jour", "jours"),
    },
    "en": {
        "minutes": ("minute", "minutes"),
        "hours": ("hour", "hours"),
        "days": ("day", "days"),
    },
    "de": {
        "minutes": ("Minute", "Minuten"),
        "hours": ("Stunde", "Stunden"),
        "days": ("Tag", "Tage"),
    },
    "es": {
        "minutes": ("minuto", "minutos"),
        "hours": ("hora", "horas"),
        "days": ("día", "días"),
    },
    "it": {
        "minutes": ("minuto", "minuti"),
        "hours": ("ora", "ore"),
        "days": ("giorno", "giorni"),
    },
}
EARLY = {
    "fr": ", avec une alerte {amount} avant",
    "en": ", with an alert {amount} before",
    "de": ", mit einem Alarm {amount} vorher",
    "es": ", con un aviso {amount} antes",
    "it": ", con un avviso {amount} prima",
}


def question(kind: str, language: str) -> str:
    return QUESTIONS[kind][language]


def is_question(text: str) -> bool:
    """One of the questions this route asks: the next message may answer it."""
    return fold(text).strip() in QUESTION_TEXTS


def understand(
    text: str, previous_answer: str = "", previous_request: str = ""
) -> Spec | None:
    """The reminder this message asks for, possibly completing an earlier request."""
    spec = parse(text)
    if spec is not None:
        return spec if spec.is_request else None
    if not previous_request or not is_question(previous_answer) or len(text) > 160:
        return None
    first = parse(previous_request)
    if first is None or not first.is_request:
        return None
    later = parse(text, first.language)
    if later is None or not later.has_time_information:
        return None
    return first.merged(later)


# ---------------------------------------------------------------- wording


def _date_text(day: date, language: str, today: date) -> str:
    from babel.dates import format_date

    pattern = DATE_FORMAT[language]
    if day.year != today.year:
        pattern += YEAR_SUFFIX[language]
    return format_date(day, pattern, locale=language)


def _time_text(moment: datetime, language: str) -> str:
    hour, minute = moment.hour, moment.minute
    if language == "fr":
        return f"{hour} h {minute:02d}"
    if language == "de":
        return f"{hour:02d}:{minute:02d} Uhr"
    if language == "en":
        suffix = "AM" if hour < 12 else "PM"
        return f"{(hour % 12) or 12}:{minute:02d} {suffix}"
    return f"{hour}:{minute:02d}"


def _at(moment: datetime, language: str) -> str:
    clock = _time_text(moment, language)
    return {
        "fr": "à " + clock,
        "en": "at " + clock,
        "de": "um " + clock,
        "es": ("a la " if moment.hour == 1 else "a las ") + clock,
        "it": ("all’" if moment.hour == 1 else "alle ") + clock,
    }[language]


def _amount(delta: timedelta, language: str) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes % 1440 == 0:
        unit, value = "days", minutes // 1440
    elif minutes % 60 == 0:
        unit, value = "hours", minutes // 60
    else:
        unit, value = "minutes", minutes
    singular, plural = UNIT_NAMES[language][unit]
    return f"{value} {singular if value == 1 else plural}"


def _weekdays(days: tuple[int, ...], language: str) -> str:
    from babel.dates import get_day_names

    names = get_day_names("wide", locale=language)
    words = [str(names[day]) for day in days]
    joint = {"fr": " et ", "en": " and ", "de": " und ", "es": " y ", "it": " e "}[
        language
    ]
    if language == "en":
        words = [word.capitalize() for word in words]
    return ", ".join(words[:-1]) + joint + words[-1] if len(words) > 1 else words[0]


def _rule_text(recurrence: Recurrence, start: datetime, language: str) -> str:
    n = recurrence.interval
    freq = recurrence.freq
    if freq == "HOURLY":
        return {
            "fr": "toutes les heures" if n == 1 else f"toutes les {n} heures",
            "en": "every hour" if n == 1 else f"every {n} hours",
            "de": "jede Stunde" if n == 1 else f"alle {n} Stunden",
            "es": "cada hora" if n == 1 else f"cada {n} horas",
            "it": "ogni ora" if n == 1 else f"ogni {n} ore",
        }[language]
    if freq == "DAILY":
        return {
            "fr": "tous les jours" if n == 1 else f"tous les {n} jours",
            "en": "every day" if n == 1 else f"every {n} days",
            "de": "jeden Tag" if n == 1 else f"alle {n} Tage",
            "es": "todos los días" if n == 1 else f"cada {n} días",
            "it": "ogni giorno" if n == 1 else f"ogni {n} giorni",
        }[language]
    if freq == "WEEKLY":
        days = recurrence.byday or (start.weekday(),)
        if days == (0, 1, 2, 3, 4) and n == 1:
            return {
                "fr": "du lundi au vendredi",
                "en": "every weekday",
                "de": "werktags",
                "es": "de lunes a viernes",
                "it": "dal lunedì al venerdì",
            }[language]
        names = _weekdays(days, language)
        if n == 1:
            return {
                "fr": "chaque " + names,
                "en": "every " + names,
                "de": "jeden " + names,
                "es": "cada " + names,
                "it": "ogni " + names,
            }[language]
        return {
            "fr": f"toutes les {n} semaines ({names})",
            "en": f"every {n} weeks ({names})",
            "de": f"alle {n} Wochen ({names})",
            "es": f"cada {n} semanas ({names})",
            "it": f"ogni {n} settimane ({names})",
        }[language]
    if freq == "MONTHLY":
        day = recurrence.bymonthday or start.day
        every = {
            "fr": "de chaque mois" if n == 1 else f"tous les {n} mois",
            "en": "of every month" if n == 1 else f"every {n} months",
            "de": "jedes Monats" if n == 1 else f"alle {n} Monate",
            "es": "de cada mes" if n == 1 else f"cada {n} meses",
            "it": "di ogni mese" if n == 1 else f"ogni {n} mesi",
        }[language]
        return {
            "fr": f"le {day} {every}",
            "en": f"on day {day} {every}",
            "de": f"am {day}. {every}",
            "es": f"el día {day} {every}",
            "it": f"il giorno {day} {every}",
        }[language]
    from babel.dates import format_date

    day_text = format_date(start.date(), MONTH_DAY_FORMAT[language], locale=language)
    return {
        "fr": "chaque année le " + day_text,
        "en": "every year on " + day_text,
        "de": "jedes Jahr am " + day_text,
        "es": "cada año el " + day_text,
        "it": "ogni anno il " + day_text,
    }[language]


def when_text(plan: Plan, now: datetime) -> str:
    """The date and time of the reminder in words, from the computed start only."""
    language = plan.language
    today = now.astimezone(plan.start.tzinfo).date()
    start = plan.start
    day = _date_text(start.date(), language, today)
    if plan.recurrence is not None:
        rule = _rule_text(plan.recurrence, start, language)
        if plan.recurrence.freq == "HOURLY":
            first = day + " " + _at(start, language)
        else:
            rule += " " + _at(start, language)
            first = day
        return {
            "fr": f"{rule}, à partir du {first}",
            "en": f"{rule}, starting {first}",
            "de": f"{rule}, ab {first}",
            "es": f"{rule}, a partir del {first}",
            "it": f"{rule}, a partire da {first}",
        }[language]
    offset = (start.date() - today).days
    at = _at(start, language)
    if offset in (0, 1):
        relative = RELATIVE_DAYS[language][offset]
        return f"{relative}, {day}, {at}"
    return {
        "fr": f"le {day} {at}",
        "en": f"{day} {at}",
        "de": f"{day}, {at}",
        "es": f"el {day} {at}",
        "it": f"{day} {at}",
    }[language]


def confirmation(plan: Plan, now: datetime, zone_assumed: bool) -> str:
    language = plan.language
    when = when_text(plan, now)
    if zone_assumed:
        when += f" ({plan.zone})"
    early = (
        EARLY[language].format(amount=_amount(plan.early, language))
        if plan.early
        else ""
    )
    title = QUOTED[language].format(plan.title) if plan.title else ""
    return CONFIRMATION[language].format(title=title, when=when, early=early)


def file_name(title: str, language: str) -> str:
    """ASCII name of the file (the download route accepts no other), from the title."""
    slug = re.sub(r"[^a-z0-9]+", "-", fold(title)).strip("-")
    if len(slug) > 48:
        slug = slug[:48].rsplit("-", 1)[0]
    return (slug or fold(DEFAULT_TITLE[language])) + ".ics"


# ------------------------------------------------------------------ turn


def zone_of(request: ChatRequest) -> tuple[str, bool]:
    """The person's IANA zone, and whether it was assumed (none was sent)."""
    if request.ui_timezone and request.ui_timezone != "UTC":
        return request.ui_timezone, False
    location = request.location
    if location is not None and location.timezone:
        return location.timezone, False
    return DEFAULT_ZONE, True


def reminder_request(request: ChatRequest) -> Spec | None:
    if request.continuation or request.project_id or request.code_mode:
        return None
    if request.has_attachments or request._scheduled_run:
        return None
    messages = request.conversation_messages
    previous_answer = previous_request = ""
    if len(messages) >= 3 and messages[-2].role == "assistant":
        previous_answer = messages[-2].text
        earlier = [m.text for m in messages[:-2] if m.role == "user"]
        previous_request = earlier[-1] if earlier else ""
    return understand(request.user_text, previous_answer, previous_request)


async def publish(content: bytes, name: str, request_id: str, timeout: float) -> str:
    """Store the file in this request's user-file area; return its download route."""
    from services.orchestrator.terminal_client import TerminalClient

    terminal = TerminalClient(request_id)
    await terminal.initialize()
    path = await terminal.upload_generated(
        name, content, timeout, max_bytes=MAX_FILE_BYTES
    )
    return DOWNLOAD_PREFIX + quote(path.lstrip("/"), safe="/")


async def reminder_turn(
    request: ChatRequest, item: Interaction, now: datetime | None = None
) -> bool:
    """Answer an explicit reminder request here; False lets the normal path answer."""
    spec = reminder_request(request)
    if spec is None:
        return False
    moment = now or datetime.now(timezone.utc)
    zone, assumed = zone_of(request)
    outcome = resolve(spec, moment, zone)
    item.state, item.task_type, item.cout_eur = "done", "reminder", 0.0
    if isinstance(outcome, Question):
        await _answer(item, question(outcome.kind, outcome.language))
        item.erreurs.append("reminder_" + outcome.kind)
        return True
    plan = outcome
    title = plan.title or DEFAULT_TITLE[plan.language]
    content = calendar_file(
        title,
        plan.start,
        plan.zone,
        early=plan.early,
        recurrence=plan.recurrence,
        now=moment,
    )
    name = file_name(title, plan.language)
    try:
        url = await publish(
            content, name, item.request_id, min(30.0, request.timeout_seconds)
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TimeoutError,
        aiohttp.ClientError,
    ) as exc:
        # D5: the failure is said, never hidden behind a link that does not exist.
        item.erreurs.append("reminder_publish_failed:" + type(exc).__name__)
        await _answer(item, FAILURE[plan.language])
        return True
    item.files, item.file_strategies = [url], ["created"]
    text = confirmation(plan, moment, assumed) + f"\n\n[{name}]({url})"
    if spec.extra:
        text += "\n\n" + EXTRA[plan.language]
    await _answer(item, text)
    return True


async def _answer(item: Interaction, text: str) -> None:
    item.reponse = text
    if sink := sink_context.get():
        await sink({"delta": {"content": text}})
