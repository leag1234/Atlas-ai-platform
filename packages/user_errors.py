"""Readable failure sentences for the chat UI.

The technical code and diagnostics stay in the error code, the logs and the
interaction record; the person reading the chat gets one sentence in their
language. Budget and quota refusals are not mapped here: they keep their
measured values (D5); the refusals of the on-demand owned server's starter are
written here with their measured values.
"""

from collections.abc import Mapping
from typing import Literal

PROVIDER_FAILURES = frozenset(
    {
        "provider_error",
        "empty_completion",
        "empty_content_after_retry",
        "incomplete_provider_answer",
        "malformed_tool_call",
        "promise_only_final_answer",
        "request_size_exceeded",
        "timeout",
    }
)

_INTERRUPTED = {
    "fr": "La réponse a été interrompue avant la fin par le service du modèle. Réessayez dans un instant.",
    "en": "The model service stopped before the answer was complete. Please try again in a moment.",
    "de": "Der Modelldienst hat die Antwort vor dem Ende abgebrochen. Bitte versuchen Sie es gleich noch einmal.",
    "es": "El servicio del modelo se detuvo antes de terminar la respuesta. Inténtelo de nuevo en un momento.",
    "it": "Il servizio del modello si è interrotto prima di completare la risposta. Riprova tra un momento.",
}
_TOO_LONG = {
    "fr": "La réponse a pris trop de temps et a été arrêtée. Réessayez, ou découpez la demande en étapes plus courtes.",
    "en": "The answer took too long and was stopped. Try again, or split the request into shorter steps.",
    "de": "Die Antwort hat zu lange gedauert und wurde gestoppt. Versuchen Sie es erneut oder teilen Sie die Anfrage in kürzere Schritte auf.",
    "es": "La respuesta tardó demasiado y se detuvo. Inténtelo de nuevo o divida la solicitud en pasos más cortos.",
    "it": "La risposta ha richiesto troppo tempo ed è stata interrotta. Riprova o dividi la richiesta in passaggi più brevi.",
}


# Any other failure of a chat answer: the person reads one sentence in their
# language; the exception, timings and costs stay in the logs and the record
# (staging 2026-11-16: an English diagnostic with the exception name, elapsed
# time and reserved cost was shown inside a French conversation).
_UNEXPECTED = {
    "fr": "La réponse n'a pas pu aboutir à cause d'une erreur inattendue. Réessayez dans un instant.",
    "en": "The answer could not be completed because of an unexpected error. Please try again in a moment.",
    "de": "Die Antwort konnte wegen eines unerwarteten Fehlers nicht abgeschlossen werden. Bitte versuchen Sie es gleich noch einmal.",
    "es": "La respuesta no pudo completarse por un error inesperado. Inténtelo de nuevo en un momento.",
    "it": "La risposta non è stata completata a causa di un errore imprevisto. Riprova tra un momento.",
}


def unexpected_failure(language: str) -> str:
    """The chat UI's sentence for a failure that is no limit, deadline or refusal."""
    return _UNEXPECTED.get(language, _UNEXPECTED["en"])


_OWNED_STARTING = {
    "fr": "Le serveur privé démarre (environ {wait}). Votre message n'a été envoyé nulle part ailleurs. Renvoyez-le dans un instant.",
    "en": "The private server is starting (about {wait}). Your message was not sent anywhere else. Send it again in a moment.",
    "de": "Der private Server startet (etwa {wait}). Ihre Nachricht wurde nirgendwo anders hin gesendet. Senden Sie sie gleich noch einmal.",
    "es": "El servidor privado se está iniciando (unos {wait}). Su mensaje no se ha enviado a ningún otro sitio. Vuelva a enviarlo en un momento.",
    "it": "Il server privato si sta avviando (circa {wait}). Il messaggio non è stato inviato altrove. Invialo di nuovo tra un momento.",
}
_OWNED_BUDGET = {
    "fr": "Le mode strict n'utilise que le serveur privé, et sa limite est atteinte : {used} sur {limit} {period}. Votre message n'a été envoyé nulle part ailleurs.",
    "en": "Strict mode runs only on the private server, and its limit is reached: {used} of {limit} {period}. Your message was not sent anywhere else.",
    "de": "Der strikte Modus nutzt nur den privaten Server, und dessen Grenze ist erreicht: {used} von {limit} {period}. Ihre Nachricht wurde nirgendwo anders hin gesendet.",
    "es": "El modo estricto solo usa el servidor privado y se ha alcanzado su límite: {used} de {limit} {period}. Su mensaje no se ha enviado a ningún otro sitio.",
    "it": "La modalità rigorosa usa solo il server privato e il suo limite è stato raggiunto: {used} su {limit} {period}. Il messaggio non è stato inviato altrove.",
}
_OWNED_START_LIMIT = {
    "fr": "Le serveur privé a déjà été démarré {used} fois dans l'heure (limite {limit}). Votre message n'a été envoyé nulle part ailleurs.",
    "en": "The private server was already started {used} times in the last hour (limit {limit}). Your message was not sent anywhere else.",
    "de": "Der private Server wurde in der letzten Stunde bereits {used}-mal gestartet (Grenze {limit}). Ihre Nachricht wurde nirgendwo anders hin gesendet.",
    "es": "El servidor privado ya se ha iniciado {used} veces en la última hora (límite {limit}). Su mensaje no se ha enviado a ningún otro sitio.",
    "it": "Il server privato è già stato avviato {used} volte nell'ultima ora (limite {limit}). Il messaggio non è stato inviato altrove.",
}
# The start was tried and failed (D5: never "starting" after a failed start). The
# hosting provider is not named on user screens.
_OWNED_UNAVAILABLE = {
    "capacity": {
        "fr": "Le serveur privé ne peut pas démarrer pour le moment : l'hébergeur n'a plus de machine de ce type disponible. Votre message n'a été envoyé nulle part ailleurs. Réessayez plus tard, ou ouvrez une nouvelle conversation en mode Standard si son contenu le permet.",
        "en": "The private server cannot start right now: the hosting provider has no machine of this type available. Your message was not sent anywhere else. Try again later, or open a new conversation in Standard mode if its content allows it.",
        "de": "Der private Server kann gerade nicht starten: Der Hoster hat keine Maschine dieses Typs frei. Ihre Nachricht wurde nirgendwo anders hin gesendet. Versuchen Sie es später erneut oder beginnen Sie ein neues Gespräch im Standardmodus, wenn der Inhalt es erlaubt.",
        "es": "El servidor privado no puede iniciarse ahora: el proveedor de alojamiento no tiene ninguna máquina de este tipo disponible. Su mensaje no se ha enviado a ningún otro sitio. Inténtelo más tarde o abra una nueva conversación en modo Estándar si su contenido lo permite.",
        "it": "Il server privato non può avviarsi ora: il fornitore di hosting non ha macchine di questo tipo disponibili. Il messaggio non è stato inviato altrove. Riprova più tardi, oppure apri una nuova conversazione in modalità Standard se il contenuto lo consente.",
    },
    "start_failed": {
        "fr": "Le serveur privé n'a pas pu démarrer. Votre message n'a été envoyé nulle part ailleurs. Réessayez plus tard, ou ouvrez une nouvelle conversation en mode Standard si son contenu le permet.",
        "en": "The private server could not start. Your message was not sent anywhere else. Try again later, or open a new conversation in Standard mode if its content allows it.",
        "de": "Der private Server konnte nicht starten. Ihre Nachricht wurde nirgendwo anders hin gesendet. Versuchen Sie es später erneut oder beginnen Sie ein neues Gespräch im Standardmodus, wenn der Inhalt es erlaubt.",
        "es": "El servidor privado no pudo iniciarse. Su mensaje no se ha enviado a ningún otro sitio. Inténtelo más tarde o abra una nueva conversación en modo Estándar si su contenido lo permite.",
        "it": "Il server privato non è riuscito ad avviarsi. Il messaggio non è stato inviato altrove. Riprova più tardi, oppure apri una nuova conversazione in modalità Standard se il contenuto lo consente.",
    },
}
_OWNED_RETRY = {
    "fr": " Réessayez dans environ {wait}.",
    "en": " Try again in about {wait}.",
    "de": " Versuchen Sie es in etwa {wait} erneut.",
    "es": " Vuelva a intentarlo dentro de unos {wait}.",
    "it": " Riprova tra circa {wait}.",
}
_PERIODS = {
    "fr": {"today": "aujourd'hui", "month": "ce mois-ci", "hour": "dans l'heure"},
    "en": {"today": "today", "month": "this month", "hour": "this hour"},
    "de": {"today": "heute", "month": "in diesem Monat", "hour": "in dieser Stunde"},
    "es": {"today": "hoy", "month": "este mes", "hour": "en esta hora"},
    "it": {"today": "oggi", "month": "questo mese", "hour": "in quest'ora"},
}


def _amount(value: object, unit: object) -> str:
    number = float(value) if isinstance(value, (int, float)) else 0.0
    shown = str(int(number)) if number.is_integer() else f"{number:.2f}"
    return shown + {"hours": " h", "EUR": " EUR"}.get(str(unit), "")


def owned_server_sentence(
    code: str, language: str, values: Mapping[str, object]
) -> str | None:
    """The readable sentence of an on-demand owned server answer (starting, or a
    starter refusal with its measured values: D5), else None."""
    from packages.owned_server import (
        BUDGET,
        START_LIMIT,
        STARTING,
        UNAVAILABLE,
        wait_text,
    )

    lang = language if language in _OWNED_STARTING else "en"
    retry = values.get("retry_after")
    wait = (
        wait_text(int(retry)) if isinstance(retry, (int, float)) and retry > 0 else ""
    )
    if code == STARTING:
        return _OWNED_STARTING[lang].format(wait=wait or "2 min")
    if code == UNAVAILABLE:
        reason = values.get("reason")
        labels = _OWNED_UNAVAILABLE.get(
            reason if isinstance(reason, str) else "",
            _OWNED_UNAVAILABLE["start_failed"],
        )
        return labels[lang]
    if code not in (BUDGET, START_LIMIT):
        return None
    labels = _OWNED_BUDGET if code == BUDGET else _OWNED_START_LIMIT
    measured, limit, unit = (values.get(k) for k in ("measured", "limit", "unit"))
    sentence = (
        labels[lang]
        .format(
            used=_amount(measured, unit) if code == BUDGET else _amount(measured, None),
            limit=_amount(limit, unit) if code == BUDGET else _amount(limit, None),
            period=_PERIODS[lang].get(str(values.get("period")), ""),
        )
        .replace(" .", ".")
    )
    return sentence + (_OWNED_RETRY[lang].format(wait=wait) if wait else "")


def ui_failure(
    code: str, language: str, values: Mapping[str, object] | None = None
) -> str | None:
    """One readable sentence for a provider failure, a deadline or an owned server
    answer, else None."""
    owned = owned_server_sentence(code, language, values or {})
    if owned is not None:
        return owned
    if code not in PROVIDER_FAILURES:
        return None
    labels = _TOO_LONG if code == "timeout" else _INTERRUPTED
    return labels.get(language, labels["en"])


# App and drawing turns (the side panel). Every failure of such a turn ends with
# one of these sentences, never with diagnostics; the code stays in the record.
_APP_FAILED = {
    "app": {
        "fr": "Je n'ai pas réussi à créer l'application cette fois-ci. Renvoyez votre message pour réessayer.",
        "en": "I could not create the app this time. Send your message again to retry.",
        "de": "Die App konnte diesmal nicht erstellt werden. Senden Sie Ihre Nachricht erneut, um es noch einmal zu versuchen.",
        "es": "No he podido crear la aplicación esta vez. Envíe de nuevo su mensaje para volver a intentarlo.",
        "it": "Non sono riuscito a creare l'app questa volta. Invia di nuovo il messaggio per riprovare.",
    },
    "drawing": {
        "fr": "Je n'ai pas réussi à faire le dessin cette fois-ci. Renvoyez votre message pour réessayer.",
        "en": "I could not make the drawing this time. Send your message again to retry.",
        "de": "Die Zeichnung konnte diesmal nicht erstellt werden. Senden Sie Ihre Nachricht erneut, um es noch einmal zu versuchen.",
        "es": "No he podido hacer el dibujo esta vez. Envíe de nuevo su mensaje para volver a intentarlo.",
        "it": "Non sono riuscito a fare il disegno questa volta. Invia di nuovo il messaggio per riprovare.",
    },
}
_APP_TOO_LONG = {
    "app": {
        "fr": "L'application a pris trop de temps à écrire et a été arrêtée. Renvoyez votre message pour réessayer, ou demandez une version plus simple.",
        "en": "Writing the app took too long and was stopped. Send your message again to retry, or ask for a simpler version.",
        "de": "Das Schreiben der App hat zu lange gedauert und wurde gestoppt. Senden Sie Ihre Nachricht erneut oder bitten Sie um eine einfachere Version.",
        "es": "Escribir la aplicación tardó demasiado y se detuvo. Envíe de nuevo su mensaje para volver a intentarlo o pida una versión más sencilla.",
        "it": "La scrittura dell'app ha richiesto troppo tempo ed è stata interrotta. Invia di nuovo il messaggio per riprovare o chiedi una versione più semplice.",
    },
    "drawing": {
        "fr": "Le dessin a pris trop de temps et a été arrêté. Renvoyez votre message pour réessayer, ou demandez un dessin plus simple.",
        "en": "The drawing took too long and was stopped. Send your message again to retry, or ask for a simpler drawing.",
        "de": "Die Zeichnung hat zu lange gedauert und wurde gestoppt. Senden Sie Ihre Nachricht erneut oder bitten Sie um eine einfachere Zeichnung.",
        "es": "El dibujo tardó demasiado y se detuvo. Envíe de nuevo su mensaje para volver a intentarlo o pida un dibujo más sencillo.",
        "it": "Il disegno ha richiesto troppo tempo ed è stato interrotto. Invia di nuovo il messaggio per riprovare o chiedi un disegno più semplice.",
    },
}
_UNCHANGED = {
    "fr": "La modification n'a pas pu être appliquée ; la version {version} reste inchangée. Renvoyez votre message pour réessayer.",
    "en": "The change could not be applied; version {version} is unchanged. Send your message again to retry.",
    "de": "Die Änderung konnte nicht übernommen werden; Version {version} bleibt unverändert. Senden Sie Ihre Nachricht erneut, um es noch einmal zu versuchen.",
    "es": "No se pudo aplicar el cambio; la versión {version} no cambia. Envíe de nuevo su mensaje para volver a intentarlo.",
    "it": "Non è stato possibile applicare la modifica; la versione {version} resta invariata. Invia di nuovo il messaggio per riprovare.",
}


def app_failure(
    kind: Literal["app", "drawing"],
    language: str,
    *,
    timeout: bool = False,
    unchanged_version: int | None = None,
) -> str:
    """One readable sentence ending a failed app or drawing turn, with a retry."""
    if unchanged_version is not None:
        return _UNCHANGED.get(language, _UNCHANGED["en"]).format(
            version=unchanged_version
        )
    labels = (_APP_TOO_LONG if timeout else _APP_FAILED)[kind]
    return labels.get(language, labels["en"])


# The render check (services/orchestrator/app_render.py) opened the app twice
# and it did not work: said plainly, with what the check saw, never delivered
# as if it worked.
_APP_RENDER = {
    "fr": "J'ai ouvert l'application pour la vérifier et elle ne fonctionnait pas ({reasons}), même après une correction ; je ne vous la donne donc pas.",
    "en": "I opened the app to check it and it did not work ({reasons}), even after one fix, so I am not handing it over.",
    "de": "Ich habe die App zur Prüfung geöffnet und sie funktionierte nicht ({reasons}), auch nicht nach einer Korrektur; deshalb gebe ich sie nicht weiter.",
    "es": "Abrí la aplicación para comprobarla y no funcionaba ({reasons}), ni siquiera tras una corrección, así que no se la entrego.",
    "it": "Ho aperto l'app per verificarla e non funzionava ({reasons}), nemmeno dopo una correzione, quindi non te la consegno.",
}
_APP_RENDER_REASONS = {
    "error": {
        "fr": "elle affichait une erreur",
        "en": "it raised an error",
        "de": "sie meldete einen Fehler",
        "es": "mostraba un error",
        "it": "segnalava un errore",
    },
    "network": {
        "fr": "elle avait besoin de fichiers d'Internet, que l'aperçu bloque",
        "en": "it needed files from the internet, which the preview blocks",
        "de": "sie brauchte Dateien aus dem Internet, die die Vorschau blockiert",
        "es": "necesitaba archivos de internet, que la vista previa bloquea",
        "it": "aveva bisogno di file da Internet, che l'anteprima blocca",
    },
    "blank": {
        "fr": "la page restait vide",
        "en": "the page stayed blank",
        "de": "die Seite blieb leer",
        "es": "la página quedaba en blanco",
        "it": "la pagina restava vuota",
    },
    "controls": {
        "fr": "ses boutons ne réagissaient pas",
        "en": "its buttons did not react",
        "de": "ihre Schaltflächen reagierten nicht",
        "es": "sus botones no respondían",
        "it": "i suoi pulsanti non rispondevano",
    },
    "frozen": {
        "fr": "elle se bloquait",
        "en": "it froze",
        "de": "sie blieb hängen",
        "es": "se quedaba bloqueada",
        "it": "si bloccava",
    },
}
_APP_RENDER_GROUPS = {
    "not_loaded": "frozen",
    "not_responding": "frozen",
    "page_error": "error",
    "control_error": "error",
    "csp_violation": "network",
    "blocked_request": "network",
    "blank_page": "blank",
    "dead_controls": "controls",
}
_RETRY = {
    "fr": "Renvoyez votre message pour réessayer, ou demandez une version plus simple.",
    "en": "Send your message again to retry, or ask for a simpler version.",
    "de": "Senden Sie Ihre Nachricht erneut oder bitten Sie um eine einfachere Version.",
    "es": "Envíe de nuevo su mensaje para volver a intentarlo o pida una versión más sencilla.",
    "it": "Invia di nuovo il messaggio per riprovare o chiedi una versione più semplice.",
}
_UNCHANGED_SHORT = {
    "fr": "La version {version} reste inchangée.",
    "en": "Version {version} is unchanged.",
    "de": "Version {version} bleibt unverändert.",
    "es": "La versión {version} no cambia.",
    "it": "La versione {version} resta invariata.",
}


def app_render_failure(
    language: str, codes: list[str], *, unchanged_version: int | None = None
) -> str:
    """An app that failed the render check twice: what was seen, and a retry."""
    language = language if language in _APP_RENDER else "en"
    groups: list[str] = []
    for code in codes:
        group = _APP_RENDER_GROUPS.get(code, "error")
        if group not in groups:
            groups.append(group)
    reasons = ", ".join(
        _APP_RENDER_REASONS[group][language] for group in (groups or ["error"])[:2]
    )
    parts = [_APP_RENDER[language].format(reasons=reasons)]
    if unchanged_version is not None:
        parts.append(_UNCHANGED_SHORT[language].format(version=unchanged_version))
    parts.append(_RETRY[language])
    return " ".join(parts)


_QUOTA = {
    "fr": "La limite d'applications et de dessins est atteinte (au plus {hourly} nouvelles versions par heure et {stored} conservées). Réessayez plus tard.",
    "en": "The limit for apps and drawings is reached (at most {hourly} new versions per hour and {stored} kept). Try again later.",
    "de": "Das Limit für Apps und Zeichnungen ist erreicht (höchstens {hourly} neue Versionen pro Stunde und {stored} gespeicherte). Versuchen Sie es später erneut.",
    "es": "Se ha alcanzado el límite de aplicaciones y dibujos (como máximo {hourly} versiones nuevas por hora y {stored} conservadas). Inténtelo más tarde.",
    "it": "È stato raggiunto il limite di app e disegni (al massimo {hourly} nuove versioni all'ora e {stored} conservate). Riprova più tardi.",
}


def app_quota(language: str, hourly: int, stored: int) -> str:
    """The storage quota of apps and drawings, with its limits (D5)."""
    return _QUOTA.get(language, _QUOTA["en"]).format(hourly=hourly, stored=stored)
