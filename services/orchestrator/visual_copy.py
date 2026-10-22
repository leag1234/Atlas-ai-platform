"""Localized presentation for deterministic visual tools."""

import re

_KEYS = (
    "qr",
    "image",
    "source",
    "payload",
    "examples",
    "categories",
    "partial",
    "failure",
    "quota",
    "rate",
    "timeout",
    "invalid",
    "unavailable",
    "url",
    "text",
    "wifi",
    "contact",
    "email",
    "sms",
    "geo",
    "event",
)
_ROWS = {
    "en": (
        "QR code",
        "Web image",
        "Source",
        "Payload",
        "Generated locally for testing; these QR codes were not retrieved from the web.",
        "Categories to test",
        "Only {found} of {requested} requested images could be retrieved and validated.",
        "The visual tool failed.",
        "Search quota exhausted.",
        "Search rate limit reached.",
        "The visual request timed out.",
        "The visual tool arguments are invalid.",
        "The image search provider is unavailable.",
        "URL",
        "Plain text",
        "Wi-Fi",
        "Contact card",
        "E-mail",
        "SMS",
        "Geolocation",
        "Calendar event",
    ),
    "fr": (
        "Code QR",
        "Image du web",
        "Source",
        "Contenu",
        "Créés localement pour les tests ; ces codes QR n’ont pas été récupérés sur le web.",
        "Catégories à tester",
        "Seules {found} images sur les {requested} demandées ont pu être récupérées et validées.",
        "L’outil visuel a échoué.",
        "Le quota de recherche est épuisé.",
        "La limite de fréquence des recherches est atteinte.",
        "Le délai de la demande visuelle est dépassé.",
        "Les paramètres de l’outil visuel sont invalides.",
        "Le fournisseur de recherche d’images est indisponible.",
        "URL",
        "Texte brut",
        "Wi-Fi",
        "Fiche de contact",
        "E-mail",
        "SMS",
        "Géolocalisation",
        "Événement de calendrier",
    ),
    "de": (
        "QR-Code",
        "Webbild",
        "Quelle",
        "Inhalt",
        "Lokal zum Testen erstellt; diese QR-Codes wurden nicht aus dem Web abgerufen.",
        "Kategorien zum Testen",
        "Nur {found} von {requested} angeforderten Bildern konnten abgerufen und validiert werden.",
        "Das Bildwerkzeug ist fehlgeschlagen.",
        "Das Suchkontingent ist aufgebraucht.",
        "Das Suchratenlimit wurde erreicht.",
        "Die Zeit für die Bildanfrage ist abgelaufen.",
        "Die Parameter des Bildwerkzeugs sind ungültig.",
        "Der Bildsuchanbieter ist nicht verfügbar.",
        "URL",
        "Klartext",
        "WLAN",
        "Kontaktkarte",
        "E-Mail",
        "SMS",
        "Geolokalisierung",
        "Kalenderereignis",
    ),
    "es": (
        "Código QR",
        "Imagen de la web",
        "Fuente",
        "Contenido",
        "Generados localmente para pruebas; estos códigos QR no se recuperaron de la web.",
        "Categorías para probar",
        "Solo se pudieron recuperar y validar {found} de las {requested} imágenes solicitadas.",
        "La herramienta visual falló.",
        "Se ha agotado la cuota de búsqueda.",
        "Se alcanzó el límite de frecuencia de búsqueda.",
        "Se agotó el tiempo de la solicitud visual.",
        "Los parámetros de la herramienta visual no son válidos.",
        "El proveedor de búsqueda de imágenes no está disponible.",
        "URL",
        "Texto sin formato",
        "Wi-Fi",
        "Tarjeta de contacto",
        "Correo electrónico",
        "SMS",
        "Geolocalización",
        "Evento de calendario",
    ),
    "it": (
        "Codice QR",
        "Immagine dal web",
        "Fonte",
        "Contenuto",
        "Generati localmente per le prove; questi codici QR non sono stati recuperati dal web.",
        "Categorie da provare",
        "È stato possibile recuperare e convalidare solo {found} delle {requested} immagini richieste.",
        "Lo strumento visivo non ha funzionato.",
        "La quota di ricerca è esaurita.",
        "È stato raggiunto il limite di frequenza delle ricerche.",
        "Il tempo per la richiesta visiva è scaduto.",
        "I parametri dello strumento visivo non sono validi.",
        "Il fornitore di ricerca immagini non è disponibile.",
        "URL",
        "Testo semplice",
        "Wi-Fi",
        "Scheda contatto",
        "E-mail",
        "SMS",
        "Geolocalizzazione",
        "Evento di calendario",
    ),
}


def copy(language: str, key: str) -> str:
    return dict(zip(_KEYS, _ROWS.get(language, _ROWS["en"]), strict=True))[key]


def failure(language: str, code: str, detail: str = "") -> str:
    key = "failure"
    if "quota" in code or "daily_search_limit" in code:
        key = "quota"
    elif "rate" in code:
        key = "rate"
    elif "timeout" in code:
        key = "timeout"
    elif "invalid" in code or "qr_" in code:
        key = "invalid"
    elif "provider" in code or "credentials" in code:
        key = "unavailable"
    message = copy(language, key)
    measured = re.search(r"measured ([0-9.]+) .*?limit ([0-9.]+)", detail)
    measured = measured or re.search(r"\(([0-9.]+) of ([0-9.]+)\)", detail)
    if measured:
        label = {
            "fr": "Valeur mesurée : {used} ; limite : {limit}.",
            "en": "Measured: {used}; limit: {limit}.",
            "de": "Gemessener Wert: {used}; Grenze: {limit}.",
            "es": "Valor medido: {used}; límite: {limit}.",
            "it": "Valore misurato: {used}; limite: {limit}.",
        }
        message += " " + label.get(language, label["en"]).format(
            used=measured[1], limit=measured[2]
        )
    return message
