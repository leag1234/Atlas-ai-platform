"""Readable failure sentences for the chat UI.

The technical code and diagnostics stay in the error code, the logs and the
interaction record; the person reading the chat gets one sentence in their
language. Budget and quota refusals are not mapped here: they keep their
measured values (D5).
"""

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
