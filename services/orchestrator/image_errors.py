"""Bounded error decoding: provider prose is never echoed into image failures."""

import json
import math
import re

import aiohttp

ERROR_BODY_LIMIT = 65536
_CODES = {
    "gpu_busy": "gpu_busy",
    "worker_busy": "gpu_busy",
    "image_worker_busy": "gpu_busy",
    "worker_starting": "worker_starting",
    "image_worker_starting": "worker_starting",
    "quota_exceeded": "quota_exceeded",
    "quota": "quota_exceeded",
    "cost_budget": "cost_budget",
    "timeout": "timeout",
    "deadline": "timeout",
    "image_deadline": "timeout",
    "configuration_missing": "configuration_missing",
    "invalid_input": "invalid_input",
    "context_exceeded": "context_exceeded",
    "provider_error": "provider_error",
}


async def read_error(response: aiohttp.ClientResponse) -> dict[str, object]:
    body = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        if len(body) + len(chunk) > ERROR_BODY_LIMIT:
            return {}
        body.extend(chunk)
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def decode_error(
    status: int, data: dict[str, object]
) -> tuple[str, str, dict[str, object]]:
    raw_code = data.get("code")
    code = _CODES.get(str(raw_code), "provider_error")
    if code == "provider_error" and status == 409:
        code = "gpu_busy"
    safe: dict[str, object] = {}
    for key in (
        "measured",
        "limit",
        "elapsed_seconds",
        "limit_seconds",
        "reserved_eur",
        "tokens",
    ):
        value = data.get(key)
        if (
            type(value) in (int, float)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and 0 <= value <= 10**12
        ):
            safe[key] = value
    if data.get("unit") in (
        "microEUR",
        "microEUR/hour",
        "milliseconds",
        "seconds",
        "requests",
        "tokens",
        "bytes",
    ):
        safe["unit"] = data["unit"]
    detail = ""
    if all(key in safe for key in ("measured", "limit", "unit")):
        detail = f"Measured {safe['measured']} {safe['unit']}; limit {safe['limit']} {safe['unit']}"
    if all(key in safe for key in ("elapsed_seconds", "limit_seconds")):
        detail = f"Timeout: {safe['elapsed_seconds']} seconds elapsed, limit {safe['limit_seconds']} seconds"
    if code == "configuration_missing":
        missing = data.get("missing")
        if isinstance(missing, list) and all(
            isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
            for name in missing
        ):
            detail = "Missing required environment variables: " + ", ".join(missing)
    return code, detail, safe


_REASONS = {
    "en": (
        "The image GPU is busy",
        "The image worker is starting",
        "Image quota exhausted",
        "Image cost budget exceeded",
        "Image generation deadline exceeded",
        "Image generation failed at the provider",
        "measured",
        "limit",
        "measurements unavailable",
    ),
    "fr": (
        "Le GPU d’image est occupé",
        "Le service d’image démarre",
        "Quota d’images épuisé",
        "Budget de génération d’image dépassé",
        "Délai de génération d’image dépassé",
        "La génération d’image a échoué chez le fournisseur",
        "mesuré",
        "limite",
        "mesures indisponibles",
    ),
    "de": (
        "Die Bild-GPU ist ausgelastet",
        "Der Bilddienst startet",
        "Bildkontingent erschöpft",
        "Bildgenerierungsbudget überschritten",
        "Zeitlimit für die Bildgenerierung überschritten",
        "Die Bildgenerierung beim Anbieter ist fehlgeschlagen",
        "gemessen",
        "Grenze",
        "Messwerte nicht verfügbar",
    ),
    "es": (
        "La GPU de imágenes está ocupada",
        "El servicio de imágenes está iniciándose",
        "Cuota de imágenes agotada",
        "Presupuesto de imágenes superado",
        "Plazo de generación de imágenes agotado",
        "La generación de imágenes ha fallado en el proveedor",
        "medido",
        "límite",
        "medidas no disponibles",
    ),
    "it": (
        "La GPU per le immagini è occupata",
        "Il servizio immagini si sta avviando",
        "Quota immagini esaurita",
        "Budget immagini superato",
        "Tempo di generazione immagini scaduto",
        "La generazione immagini è fallita presso il provider",
        "misurato",
        "limite",
        "misure non disponibili",
    ),
}


def failure_message(
    language: str, code: str, status: int, data: dict[str, object]
) -> str:
    words = _REASONS.get(language, _REASONS["en"])
    index = {
        "gpu_busy": 0,
        "worker_starting": 1,
        "quota_exceeded": 2,
        "cost_budget": 3,
        "timeout": 4,
    }.get(code, 5)
    message = words[index]
    if all(key in data for key in ("measured", "limit", "unit")):
        message += f": {words[6]} {data['measured']} {data['unit']}; {words[7]} {data['limit']} {data['unit']}"
    elif all(key in data for key in ("elapsed_seconds", "limit_seconds")):
        message += f": {words[6]} {data['elapsed_seconds']} s; {words[7]} {data['limit_seconds']} s"
    elif code in {"quota_exceeded", "cost_budget", "timeout"}:
        message += ": " + words[8]
    return message + f" (HTTP {status})."
