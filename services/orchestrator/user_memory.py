"""Explicit personal-memory control and ordinary-chat context; no model writes."""

from __future__ import annotations

import json
from typing import Any

import aiohttp

from packages.context_usage import register, tokens
from packages.user_memory import (
    MemoryWrite,
    command,
    inferred_kind,
    normalized,
    projection,
    select,
)
from packages.user_memory_auth import service_secret
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from services.retrieval.project_identity import ProjectError


async def exchange(
    subject: str,
    method: str = "GET",
    path: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from services.orchestrator.chat_pipeline import retrieval_url
    from services.orchestrator.project_ui import bounded_json

    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=5)
    ) as client:
        async with client.request(
            method,
            retrieval_url()
            + "/internal/atlas/user-memory"
            + ("/" + path if path else ""),
            json=data,
            headers={
                "X-Atlas-Memory-Secret": service_secret(),
                "X-Atlas-Memory-Subject": subject,
            },
            allow_redirects=False,
        ) as reply:
            result = await bounded_json(reply)
            if reply.status != 200:
                raise ProjectError(
                    reply.status,
                    str(result.get("error", "memory_unavailable")),
                    str(result.get("message", "Personal memory is unavailable.")),
                )
            return result


# Fixed replies of the memory commands, in the language of the question.
MESSAGES: dict[str, dict[str, str]] = {
    "signin": {
        "en": "Sign in to use personal memory. Nothing was saved.",
        "fr": "Connectez-vous pour utiliser la mémoire personnelle. Rien n'a été enregistré.",
        "de": "Melden Sie sich an, um das persönliche Gedächtnis zu nutzen. Nichts wurde gespeichert.",
        "es": "Inicia sesión para usar la memoria personal. No se ha guardado nada.",
        "it": "Accedi per usare la memoria personale. Non è stato salvato nulla.",
    },
    "unavailable": {
        "en": "Personal memory is unavailable. Nothing was saved.",
        "fr": "La mémoire personnelle est indisponible. Rien n'a été enregistré.",
        "de": "Das persönliche Gedächtnis ist nicht verfügbar. Nichts wurde gespeichert.",
        "es": "La memoria personal no está disponible. No se ha guardado nada.",
        "it": "La memoria personale non è disponibile. Non è stato salvato nulla.",
    },
    "list": {
        "en": "Personal memory:",
        "fr": "Ce que je retiens sur vous :",
        "de": "Persönliches Gedächtnis:",
        "es": "Memoria personal:",
        "it": "Memoria personale:",
    },
    "empty": {
        "en": "I have no saved memories about you.",
        "fr": "Je n'ai rien enregistré sur vous.",
        "de": "Ich habe nichts über Sie gespeichert.",
        "es": "No tengo nada guardado sobre ti.",
        "it": "Non ho salvato nulla su di te.",
    },
    "disabled_note": {
        "en": "Personal memory is disabled and is not used in answers.",
        "fr": "La mémoire personnelle est désactivée et n'est pas utilisée dans les réponses.",
        "de": "Das persönliche Gedächtnis ist deaktiviert und wird in Antworten nicht verwendet.",
        "es": "La memoria personal está desactivada y no se usa en las respuestas.",
        "it": "La memoria personale è disattivata e non viene usata nelle risposte.",
    },
    "disabled": {
        "en": "Personal memory is disabled. Nothing was saved.",
        "fr": "La mémoire personnelle est désactivée. Rien n'a été enregistré.",
        "de": "Das persönliche Gedächtnis ist deaktiviert. Nichts wurde gespeichert.",
        "es": "La memoria personal está desactivada. No se ha guardado nada.",
        "it": "La memoria personale è disattivata. Non è stato salvato nulla.",
    },
    "too_long": {
        "en": "Memory text: {n} characters; limit 500. Nothing was saved.",
        "fr": "Texte à retenir : {n} caractères ; limite 500. Rien n'a été enregistré.",
        "de": "Text: {n} Zeichen; Grenze 500. Nichts wurde gespeichert.",
        "es": "Texto: {n} caracteres; límite 500. No se ha guardado nada.",
        "it": "Testo: {n} caratteri; limite 500. Non è stato salvato nulla.",
    },
    "saved": {
        "en": "Saved to your memory: {text}",
        "fr": "C'est noté, je m'en souviendrai : {text}",
        "de": "Gespeichert: {text}",
        "es": "Guardado en tu memoria: {text}",
        "it": "Salvato nella tua memoria: {text}",
    },
    "forgotten": {
        "en": "Forgotten from your memory: {text}",
        "fr": "C'est oublié : {text}",
        "de": "Vergessen: {text}",
        "es": "Olvidado: {text}",
        "it": "Dimenticato: {text}",
    },
    "ambiguous": {
        "en": "Several memories match. Delete the intended entry in Memory; nothing was deleted.",
        "fr": "Plusieurs souvenirs correspondent. Supprimez le bon dans Mémoire ; rien n'a été supprimé.",
        "de": "Mehrere Einträge passen. Löschen Sie den richtigen unter Gedächtnis; nichts wurde gelöscht.",
        "es": "Coinciden varios recuerdos. Borra el correcto en Memoria; no se ha borrado nada.",
        "it": "Più ricordi corrispondono. Elimina quello giusto in Memoria; non è stato eliminato nulla.",
    },
    "no_match": {
        "en": "No saved memory matches that text. Nothing was deleted.",
        "fr": "Aucun souvenir ne correspond à ce texte. Rien n'a été supprimé.",
        "de": "Kein gespeicherter Eintrag passt zu diesem Text. Nichts wurde gelöscht.",
        "es": "Ningún recuerdo coincide con ese texto. No se ha borrado nada.",
        "it": "Nessun ricordo corrisponde a quel testo. Non è stato eliminato nulla.",
    },
}


def message(request: ChatRequest, key: str, **values: object) -> str:
    from packages.language import question_language

    texts = MESSAGES[key]
    lang = question_language(request.user_text or "", request.lang)
    return texts.get(lang, texts["en"]).format(**values)


async def prepare(request: ChatRequest, item: Interaction) -> bool:
    # Defense in depth: compatibility project commands have already resolved scope.
    if request.project_id or request.org_id or request.code_mode:
        return False
    subject = request.__dict__.get("daily_user", "unknown")
    intent = command(request.user_text) if not request.continuation else None
    if subject == "unknown":
        if not intent:
            return False
        answer = message(request, "signin")
    elif not service_secret():
        if not intent:
            return False
        answer = message(request, "unavailable")
    else:
        try:
            view = await exchange(subject)
            entries = view["entries"]
            if intent:
                action, text = intent
                if action == "list":
                    answer = (
                        message(request, "list")
                        + "\n"
                        + "\n".join("- " + e["text"] for e in entries)
                        if entries
                        else message(request, "empty")
                    )
                    if not view["effective_enabled"]:
                        answer += "\n" + message(request, "disabled_note")
                elif action == "save":
                    if not view["effective_enabled"]:
                        answer = message(request, "disabled")
                    elif not text or len(text) > 500:
                        answer = message(request, "too_long", n=len(text))
                    else:
                        value = MemoryWrite(text=text, kind=inferred_kind(text))
                        entry = await exchange(subject, "POST", data=value.model_dump())
                        answer = message(request, "saved", text=entry["text"])
                else:
                    matches = [
                        e for e in entries if normalized(text) == normalized(e["text"])
                    ]
                    if not matches:
                        matches = [
                            e
                            for e in entries
                            if normalized(text)
                            and normalized(text) in normalized(e["text"])
                        ]
                    if len(matches) == 1:
                        await exchange(subject, "DELETE", matches[0]["id"])
                        answer = message(request, "forgotten", text=matches[0]["text"])
                    elif matches:
                        answer = message(request, "ambiguous")
                    else:
                        answer = message(request, "no_match")
            else:
                selected = (
                    select(entries, request.user_text)
                    if view["effective_enabled"]
                    else []
                )
                if selected:
                    content = projection(selected)
                    register(content, {"pinned_memory": json.loads(content)})
                    request.messages.insert(
                        0, ChatMessage(role="user", content=content)
                    )
                    item.context_manifest["sources"] = [
                        dict(kind="user_memory", id=e["id"]) for e in selected
                    ]
                    item.context_manifest["user_memory_tokens"] = tokens(content)
                return False
        except (ProjectError, aiohttp.ClientError, TimeoutError) as exc:
            # Do not silently generate a personalized answer when the store failed.
            answer = (
                str(exc.body["message"])
                if isinstance(exc, ProjectError)
                else "Personal memory is unavailable. Please retry."
            )
    item.reponse, item.state = answer, "done"
    if sink := sink_context.get():
        await sink({"delta": {"content": answer}, "memory": True})
    return True
