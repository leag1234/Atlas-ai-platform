"""Localized presentation of evaluated public places; no network or user fix."""

from datetime import datetime, tzinfo
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from babel.dates import format_date
import phonenumbers

# Product copy, not model instructions. Order is shared across supported locales.
_KEYS = (
    "open closed unknown closing opening at tomorrow on soon no_close address phone "
    "open_group later_group unknown_group distance far warning footer empty widen web"
).split()
_COPY = {
    "fr": (
        "ouvert",
        "fermé",
        "Horaires non renseignés",
        "ferme",
        "ouvre",
        "à",
        "demain",
        "le",
        "ferme bientôt",
        "fermeture non indiquée",
        "Adresse non renseignée",
        "Téléphone :",
        "Ouverts maintenant",
        "Ouverture plus tard",
        "Horaires non renseignés",
        "Distances approximatives à vol d’oiseau ; horaires locaux selon OpenStreetMap.",
        "Les lieux indiqués ouverts sont éloignés (plus de 1,5 km) ; le trajet à pied peut être plus long. Souhaitez-vous élargir la recherche ou réduire la zone ?",
        "Certains résultats ou horaires restent incertains ; confirmez par téléphone.",
        "Position approximative, utilisée pour cette demande uniquement · Données © contributeurs OpenStreetMap (ODbL)",
        "Aucun lieu OSM trouvé ; résultats web à confirmer, distances et horaires inconnus.",
        "Indiquez une commune ou essayez un rayon plus large.",
        "Source web (à confirmer)",
    ),
    "en": (
        "open",
        "closed",
        "Hours not listed",
        "closes",
        "opens",
        "at",
        "tomorrow",
        "on",
        "closing soon",
        "no closing time listed",
        "Address not listed",
        "Phone:",
        "Open now",
        "Opening later",
        "Hours not listed",
        "Approximate straight-line distances; local hours according to OpenStreetMap.",
        "The places listed open are far away (over 1.5 km); walking may take a longer route. Would you like to widen the search or narrow the area?",
        "Some results or hours remain uncertain; confirm by phone.",
        "Approximate location, used for this request only · Data © OpenStreetMap contributors (ODbL)",
        "No OSM places found; web results need confirmation, distances and hours are unknown.",
        "Enter a town or try a larger radius.",
        "Web source (please confirm)",
    ),
    "de": (
        "geöffnet",
        "geschlossen",
        "Öffnungszeiten unbekannt",
        "schließt",
        "öffnet",
        "um",
        "morgen",
        "am",
        "schließt bald",
        "Schließzeit unbekannt",
        "Adresse unbekannt",
        "Telefon:",
        "Jetzt geöffnet",
        "Öffnet später",
        "Öffnungszeiten unbekannt",
        "Ungefähre Entfernungen in Luftlinie; lokale Öffnungszeiten laut OpenStreetMap.",
        "Die als geöffnet angegebenen Orte sind weit entfernt (über 1,5 km); der Fußweg kann länger sein. Möchten Sie die Suche erweitern oder das Gebiet verkleinern?",
        "Einige Angaben sind unsicher; bitte telefonisch bestätigen lassen.",
        "Ungefährer Standort, nur für diese Anfrage verwendet · Daten © OpenStreetMap-Mitwirkende (ODbL)",
        "Keine OSM-Orte gefunden; Web-Ergebnisse bitte bestätigen, Entfernungen und Öffnungszeiten unbekannt.",
        "Geben Sie einen Ort an oder erweitern Sie den Suchradius.",
        "Webquelle (bitte bestätigen)",
    ),
    "es": (
        "abierto",
        "cerrado",
        "Horario desconocido",
        "cierra",
        "abre",
        "a las",
        "mañana",
        "el",
        "cierra pronto",
        "cierre no indicado",
        "Dirección desconocida",
        "Teléfono:",
        "Abiertos ahora",
        "Abren más tarde",
        "Horario desconocido",
        "Distancias aproximadas en línea recta; horarios locales según OpenStreetMap.",
        "Los lugares indicados como abiertos están lejos (más de 1,5 km); el recorrido a pie puede ser mayor. ¿Quiere ampliar la búsqueda o reducir la zona?",
        "Algunos datos son inciertos; confirme por teléfono.",
        "Ubicación aproximada, utilizada solo para esta solicitud · Datos © colaboradores de OpenStreetMap (ODbL)",
        "No se encontraron lugares OSM; confirme los resultados web, distancias y horarios desconocidos.",
        "Indique una localidad o amplíe el radio.",
        "Fuente web (por confirmar)",
    ),
    "it": (
        "aperto",
        "chiuso",
        "Orari sconosciuti",
        "chiude",
        "apre",
        "alle",
        "domani",
        "il",
        "chiude presto",
        "chiusura non indicata",
        "Indirizzo sconosciuto",
        "Telefono:",
        "Aperti ora",
        "Apertura più tardi",
        "Orari sconosciuti",
        "Distanze approssimative in linea d’aria; orari locali secondo OpenStreetMap.",
        "I luoghi indicati come aperti sono lontani (oltre 1,5 km); il percorso a piedi può essere più lungo. Vuole ampliare la ricerca o restringere la zona?",
        "Alcuni dati sono incerti; confermi per telefono.",
        "Posizione approssimativa, usata solo per questa richiesta · Dati © collaboratori OpenStreetMap (ODbL)",
        "Nessun luogo OSM trovato; risultati web da confermare, distanze e orari sconosciuti.",
        "Indichi una località o ampli il raggio.",
        "Fonte web (da confermare)",
    ),
}


def labels(language: str) -> dict[str, str]:
    return dict(zip(_KEYS, _COPY.get(language, _COPY["en"]), strict=True))


def safe_text(value: Any) -> str:
    text = re.sub(r"https?://\S+", "", str(value))
    return re.sub(r"[\[\]<>`*\n\r]", "", text)[:400]


def distance_label(meters: int, language: str) -> str:
    if meters < 1000:
        return f"≈ {meters} m"
    value = f"{meters / 1000:.1f}".rstrip("0").rstrip(".")
    return "≈ " + (value.replace(".", ",") if language != "en" else value) + " km"


def transition(
    value: str | None, hours: dict[str, Any], evaluated: str, language: str
) -> str:
    if not value:
        return ""
    try:
        event = datetime.fromisoformat(value)
        if event.tzinfo is None:
            return ""
        try:
            zone: tzinfo = ZoneInfo(hours.get("timezone") or "")
        except (ZoneInfoNotFoundError, ValueError):
            zone = event.tzinfo
        event = event.astimezone(zone)
        current = (
            datetime.fromisoformat(evaluated).astimezone(zone) if evaluated else None
        )
    except (ValueError, TypeError):
        return ""
    copy = labels(language)
    days = (event.date() - current.date()).days if current else None
    day = (
        ""
        if days == 0
        else copy["tomorrow"]
        if days == 1
        else (
            copy["on"] + " " + format_date(event.date(), format="long", locale=language)
        )
    )
    if language == "fr":
        clock = f"{event.hour} h {event.minute:02}"
    elif language == "en":
        clock = f"{event.hour % 12 or 12}:{event.minute:02} {'am' if event.hour < 12 else 'pm'}"
    else:
        clock = f"{event.hour}:{event.minute:02}"
    return " ".join(filter(None, (day, copy["at"], clock)))


def phone_label(value: str, country: str) -> str:
    # Preserve uncertain source data rather than guess a country from language.
    formatted = []
    for raw in value.split(";")[:3]:
        try:
            number = phonenumbers.parse(raw, country.upper() or None)
            if phonenumbers.is_possible_number(number):
                raw = phonenumbers.format_number(
                    number, phonenumbers.PhoneNumberFormat.INTERNATIONAL
                )
        except phonenumbers.NumberParseException:
            pass
        formatted.append(safe_text(raw.strip()))
    return " / ".join(formatted)


def web_rows(check: dict[str, Any]) -> list[dict[str, Any]]:
    data = check.get("data", check)
    rows = data.get("results", []) if isinstance(data, dict) else []
    return (
        [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )


def render(
    result: dict[str, Any], language: str, sources: list[str] | None = None
) -> str:
    language = language if language in _COPY else "en"
    copy = labels(language)
    places = result["places"][:5]
    numbered = dict.fromkeys(sources or [])

    def link(label: str, value: Any) -> str:
        url = str(value or "")
        if not url.startswith(("https://", "http://")) or any(
            c in url for c in "\n\r()<> []\\"
        ):
            return ""
        numbered.setdefault(url, None)
        return f"[{label}]({url}) [{list(numbered).index(url) + 1}]"

    if not places:
        lines = [copy["empty"]]
        for row in web_rows(result.get("web_fallback", {}))[:5]:
            lines.append(
                f"- **{safe_text(row.get('title', ''))}** — {safe_text(row.get('snippet', ''))} {link(copy['web'], row.get('link'))}"
            )
        lines.extend((copy["widen"], copy["footer"]))
        return "\n\n".join(lines)
    lines = [copy["distance"]]
    opened = [p for p in places if p["opening"]["status"] == "open"]
    if not opened and language == "en":
        lines.append(
            "0 place(s) confirmed open in these results; check the later openings and unknown hours below."
        )
    if opened and min(p["distance_m"] for p in opened) > 1500:
        lines.append(copy["far"])
    for status, heading in (
        ("open", "open_group"),
        ("closed", "later_group"),
        ("unknown", "unknown_group"),
    ):

        def section(place: dict[str, Any]) -> str:
            hours = place["opening"]
            if hours["status"] == "open":
                return "open"
            return (
                "closed"
                if hours["status"] == "closed" and hours.get("next_opening")
                else "unknown"
            )

        group = [p for p in places if section(p) == status]
        if not group:
            continue
        lines.append("**" + copy[heading] + "**")
        for place in group:
            hours = place["opening"]
            state = copy[hours["status"]]
            closing = transition(
                hours.get("closing_time"),
                hours,
                result.get("evaluated_at", ""),
                language,
            )
            opening = transition(
                hours.get("next_opening"),
                hours,
                result.get("evaluated_at", ""),
                language,
            )
            if status == "open":
                state += ", " + (
                    copy["closing"] + " " + closing if closing else copy["no_close"]
                )
                if hours.get("closing_soon"):
                    state += " (" + copy["soon"] + ")"
            elif status == "closed" and opening:
                state += ", " + copy["opening"] + " " + opening
            address = safe_text(place.get("address", "")) or copy["address"]
            entry = f"- **{safe_text(place['name'])}** — {distance_label(place['distance_m'], language)} — {state}.\n  {address}."
            if place.get("phone"):
                entry += (
                    "  \n  "
                    + copy["phone"]
                    + " "
                    + phone_label(place["phone"], place.get("country", ""))
                )
            entry += "  \n  " + link("OpenStreetMap", place["source"])
            for source in web_rows(place.get("web_check", {}))[:1]:
                entry += "  \n  " + link(copy["web"], source.get("link"))
            lines.append(entry)
    if result.get("warnings"):
        lines.append(copy["warning"])
    lines.append(copy["footer"])
    return "\n\n".join(lines)
