"""Service-owned activity text for the stable guest interface."""

# These are UI labels, independent of model instructions and provider diagnostics.
LABELS = {
    "fr": (
        "R\u00e9flexion\u2026",
        "R\u00e9daction\u2026",
        "Recherche web\u2026",
        "Lecture de la page\u2026",
        "Recherche documentaire\u2026",
        "Calcul\u2026",
        "Cr\u00e9ation de l'image\u2026",
        "Lecture du document\u2026",
        "Synth\u00e8se\u2026",
        "R\u00e9ponse termin\u00e9e",
        "La r\u00e9ponse a \u00e9t\u00e9 interrompue.",
    ),
    "en": (
        "Thinking\u2026",
        "Writing\u2026",
        "Searching the web\u2026",
        "Reading the page\u2026",
        "Searching documents\u2026",
        "Calculating\u2026",
        "Creating the image\u2026",
        "Reading the document\u2026",
        "Summarizing\u2026",
        "Answer complete",
        "The response was interrupted.",
    ),
    "de": (
        "Nachdenken\u2026",
        "Antwort schreiben\u2026",
        "Websuche\u2026",
        "Seite lesen\u2026",
        "Dokumente durchsuchen\u2026",
        "Berechnen\u2026",
        "Bild erstellen\u2026",
        "Dokument lesen\u2026",
        "Zusammenfassen\u2026",
        "Antwort abgeschlossen",
        "Die Antwort wurde unterbrochen.",
    ),
    "es": (
        "Reflexi\u00f3n\u2026",
        "Escribiendo\u2026",
        "Buscando en la web\u2026",
        "Leyendo la p\u00e1gina\u2026",
        "Buscando documentos\u2026",
        "Calculando\u2026",
        "Creando la imagen\u2026",
        "Leyendo el documento\u2026",
        "Resumiendo\u2026",
        "Respuesta completada",
        "La respuesta se ha interrumpido.",
    ),
    "it": (
        "Riflessione\u2026",
        "Scrittura\u2026",
        "Ricerca web\u2026",
        "Lettura della pagina\u2026",
        "Ricerca nei documenti\u2026",
        "Calcolo\u2026",
        "Creazione dell'immagine\u2026",
        "Lettura del documento\u2026",
        "Sintesi\u2026",
        "Risposta completata",
        "La risposta \u00e8 stata interrotta.",
    ),
}
ACTIVITIES = (
    "thinking",
    "answering",
    "web_search",
    "web_fetch",
    "rag_search",
    "calculator",
    "generate_image",
    "reading_document",
    "synthesizing_document",
    "done",
    "failed",
)


def activity_label(language: str, activity: str) -> str:
    """Never expose unknown tool identifiers or provider fallback diagnostics."""
    aliases = {
        "document_read": "reading_document",
        "synthesized": "synthesizing_document",
    }
    activity = aliases.get(activity, activity)
    index = ACTIVITIES.index(activity) if activity in ACTIVITIES else 0
    return LABELS.get(language, LABELS["en"])[index]
