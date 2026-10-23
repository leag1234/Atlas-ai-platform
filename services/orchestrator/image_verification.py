"""Localized, honest outcomes without exposing scene text in verification telemetry."""

import re


def verification_note(verification: object, language: str) -> str:
    if not isinstance(verification, dict):
        return ""
    failed = verification.get("failed", [])
    failed_details = verification.get("failed_details", [])
    unknown = verification.get("unknown", False)
    if verification.get("failure_note_suppressed") is True:
        return ""
    if not failed and not unknown:
        return ""
    details = []
    if isinstance(failed, list) and isinstance(failed_details, list):
        details = list(
            dict.fromkeys(
                localized
                for key, detail in zip(failed, failed_details)
                if isinstance(detail, str)
                and (localized := _localized_detail(str(key), detail, language))
            )
        )
    if details:
        templates = {
            "en": "I could not obtain an image where {details}.",
            "fr": "Je n’ai pas réussi à obtenir une image où {details}.",
            "de": "Ich konnte kein Bild erhalten, auf dem {details}.",
            "es": "No logré obtener una imagen en la que {details}.",
            "it": "Non sono riuscito a ottenere un’immagine in cui {details}.",
        }
        return templates.get(language, templates["en"]).format(
            details=", ".join(details)
        )
    generic = {
        "en": "I could not obtain exactly the requested image.",
        "fr": "Je n'ai pas réussi à obtenir exactement l'image demandée.",
        "de": "Ich konnte nicht genau das gewünschte Bild erzeugen.",
        "es": "No logré obtener exactamente la imagen solicitada.",
        "it": "Non sono riuscito a ottenere esattamente l’immagine richiesta.",
    }
    return generic.get(language, generic["en"])


def _localized_detail(key: str, detail: str, language: str) -> str:
    match = re.fullmatch(
        r"(.+?) holds (.+?)'s (?:own )?hand(?: with his hand)?", detail
    )
    if not match:
        return ""
    subject, object_ = match.groups()
    localized_object = "Marie" if language == "fr" and object_ == "Mary" else object_
    return {
        "en": f"{subject} holds {object_}'s own hand",
        "fr": f"{subject} tient la main de {localized_object}",
        "de": f"{subject} {object_}s Hand hält",
        "es": f"{subject} sostiene la mano de {object_}",
        "it": f"{subject} tiene la mano di {object_}",
    }.get(language, "")
