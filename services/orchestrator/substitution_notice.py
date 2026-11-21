"""The discreet notice of a turn that another model answered (C15).

When the provider of the selected model fails and the gateway moves the call to
its configured fallback, the person sees which model answered: never a silent
substitution. The notice is a status line, like the other turn notices.
"""

NOTICES = {
    "en": "Answered by {model}: the selected model's provider failed",
    "fr": "Réponse de {model} : le fournisseur du modèle choisi a échoué",
    "de": "Antwort von {model}: der Anbieter des gewählten Modells ist ausgefallen",
    "es": "Respuesta de {model}: el proveedor del modelo elegido ha fallado",
    "it": "Risposta di {model}: il fornitore del modello scelto non ha risposto",
}


def substitution_notice(language: str, model: str) -> str:
    """The notice in the interface language (English when it has none)."""
    return NOTICES.get(language, NOTICES["en"]).format(model=model)


def turn_substitution(
    provider_fallback: bool, provider_model: str, language: str
) -> str:
    """The notice of a turn served after a provider fallback, else ""."""
    if not provider_fallback or not provider_model:
        return ""
    return substitution_notice(language, provider_model)
