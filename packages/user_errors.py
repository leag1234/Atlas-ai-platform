"""Readable failure sentences for the chat UI.

The technical code and diagnostics stay in the error code, the logs and the
interaction record; the person reading the chat gets one sentence in their
language. Budget and quota refusals are not mapped here: they keep their
measured values (D5).
"""

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


def ui_failure(code: str, language: str) -> str | None:
    """One readable sentence for a provider failure or deadline, else None."""
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
