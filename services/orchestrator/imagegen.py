"""Image generation is routed through the gateway, including SSE chats."""

import asyncio
import json
from pathlib import Path
import os
from packages.ports import atlas_ports
import time
import re
from decimal import Decimal
from packages.images import ImageURL
from packages.imagegen import ImagePrompt
from packages.image_limits import image_limit, refusal
from packages.limits import ProviderLimitError
from services.orchestrator.image_store import store
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.stream_client import sink_context
from services.orchestrator.deadline import infrastructure_startup
from services.orchestrator.image_errors import failure_message


def startup_failure(
    language: str, code: str, status: int | None, seconds: float
) -> str:
    """Give an image-start failure a safe, final user-facing explanation."""
    timeout_seconds = max(1, round(seconds))
    if code == "configuration_missing":
        reason = {
            "fr": "le service n’est pas configuré",
            "en": "the service is not configured",
            "de": "der Dienst nicht konfiguriert ist",
            "es": "el servicio no está configurado",
            "it": "il servizio non è configurato",
        }
    elif code == "timeout":
        reason = {
            "fr": f"délai d’attente après {timeout_seconds} secondes",
            "en": f"timeout after {timeout_seconds} seconds",
            "de": f"Zeitüberschreitung nach {timeout_seconds} Sekunden",
            "es": f"tiempo de espera agotado después de {timeout_seconds} segundos",
            "it": f"timeout dopo {timeout_seconds} secondi",
        }
    elif code == "image_unavailable":
        reason = {
            "fr": "le service d’image est indisponible pour le moment",
            "en": "the image service is unavailable right now",
            "de": "der Bilddienst derzeit nicht verfügbar ist",
            "es": "el servicio de imágenes no está disponible ahora",
            "it": "il servizio immagini non è disponibile al momento",
        }
    else:
        http = f" (HTTP {status})" if status is not None else ""
        reason = {
            "fr": "fournisseur indisponible" + http,
            "en": "provider unavailable" + http,
            "de": "Anbieter nicht verfügbar" + http,
            "es": "proveedor no disponible" + http,
            "it": "provider non disponibile" + http,
        }
    labels = {
        "fr": "La génération d’image n’a pas pu démarrer : {reason}. Coût de démarrage mesuré : 0,00 EUR ; rien n’a été facturé. Réessayez dans quelques minutes.",
        "en": "Image generation could not start: {reason}. Measured startup cost: EUR 0.00; nothing was charged. Please retry in a few minutes.",
        "de": "Die Bildgenerierung konnte nicht starten: {reason}. Gemessene Startkosten: 0,00 EUR; es wurde nichts berechnet. Bitte versuchen Sie es in ein paar Minuten erneut.",
        "es": "No se pudo iniciar la generación de imágenes: {reason}. Coste de inicio medido: 0,00 EUR; no se ha cobrado nada. Vuelva a intentarlo en unos minutos.",
        "it": "Non è stato possibile avviare la generazione di immagini: {reason}. Costo di avvio misurato: 0,00 EUR; non è stato addebitato nulla. Riprovi tra qualche minuto.",
    }
    selected = language if language in labels else "en"
    return labels[selected].format(reason=reason[selected])


async def start_on_demand(gateway: str, item: Interaction) -> None:
    """Start once more after a transient provider failure, never after a budget refusal."""
    try:
        await GatewayModel.post(gateway + "/images/start", {"timeout": 895}, 900)
    except GatewayError as exc:
        if exc.code != "configuration_missing" and exc.status in {502, 503, 504}:
            item.provider_retries += 1
            await asyncio.sleep(20)
            await GatewayModel.post(gateway + "/images/start", {"timeout": 895}, 900)
            return
        raise


async def process_image(
    prompt: str,
    item: Interaction,
    seed: int | None = None,
    *,
    language: str,
    dialogue: list[str] | None = None,
    budget: Decimal = Decimal("0.05"),
    timeout: float = 118,
) -> None:
    started = time.monotonic()
    operation_timeout = timeout
    retry_cost = Decimal(0)
    generating = False
    item.task_type, item.modele_utilise, item.route_decision = (
        "imagegen",
        "local",
        "simple",
    )
    if category := image_limit(prompt):
        item.reponse = refusal(category, language)
        item.state, item.cout_eur = "done", 0.0
        item.rejection = {"code": "image_limit_" + category, "message": item.reponse}
        if sink := sink_context.get():
            await sink({"delta": {"content": item.reponse}})
        return
    ImagePrompt(prompt=prompt, seed=seed)
    item.cout_eur = float(budget)
    if not budget.is_finite():
        raise RuntimeError("cost_budget: expected a finite monetary amount")
    if not 0 < budget <= Decimal("0.05"):
        raise ProviderLimitError(
            "cost_budget", int(budget * 1000000), 50000, "microEUR"
        )
    if not 0 < timeout <= 120:
        raise ProviderLimitError(
            "deadline", int(timeout * 1000), 120000, "milliseconds"
        )
    try:
        await image_progress("generating")
        if os.environ.get("ATLAS_IMAGE_ON_DEMAND") == "1":
            # Provision through the gateway; M13 accounts for loading separately.
            gateway = os.environ.get(
                "ATLAS_GATEWAY_URL", atlas_ports().url("gateway")
            ).rstrip("/")
            health = await GatewayModel.post(
                gateway + "/images/ready", {}, min(3, timeout)
            )
            if health.get("ready") is not True:
                await image_progress("starting")
                if sink := sink_context.get():
                    notices = json.loads(Path("prompts/image-start.json").read_text())
                    await sink(
                        {
                            "delta": {"content": notices[language] + "\n\n"},
                            "image_start_notice": True,
                        }
                    )
                loading = time.monotonic()
                try:
                    async with infrastructure_startup(900):
                        await start_on_demand(gateway, item)
                except (GatewayError, TimeoutError) as exc:
                    # A budget refusal is never an outage (D5); a missing configuration keeps its
                    # explicit owner diagnostic (M20): neither is rewritten for the user.
                    if isinstance(exc, GatewayError) and exc.code in {
                        "cost_budget",
                        "configuration_missing",
                    }:
                        raise
                    code = exc.code if isinstance(exc, GatewayError) else "timeout"
                    status = exc.status if isinstance(exc, GatewayError) else None
                    match = re.search(
                        r"limit ([0-9.]+) seconds", str(getattr(exc, "detail", ""))
                    )
                    seconds = (
                        float(match.group(1)) if match else time.monotonic() - loading
                    )
                    item.cout_eur = 0.0
                    item.erreurs.append("image_start_" + code)
                    item.rejection = {"code": "image_start_" + code, "message": ""}
                    item.reponse = startup_failure(language, code, status, seconds)
                    item.rejection["message"] = item.reponse
                    await image_progress("failed", item.reponse)
                    item.state = "done"
                    sink = sink_context.get()
                    if sink is not None:
                        await sink({"delta": {"content": item.reponse}})
                    return
                finally:
                    item.startup_seconds = time.monotonic() - loading
            timeout -= time.monotonic() - started - item.startup_seconds
            if timeout <= 0:
                raise TimeoutError("image_worker_start_timeout")
        generating = True
        await image_progress("generating")
        for attempt in range(2):
            remaining = operation_timeout - (
                time.monotonic() - started - item.startup_seconds
            )
            if remaining <= 0:
                raise TimeoutError("image_deadline")
            try:
                response = await GatewayModel.post(
                    os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))
                    + "/images/generate",
                    {
                        "prompt": prompt,
                        **(
                            {"dialogue": dialogue}
                            if dialogue and len(dialogue) > 1
                            else {}
                        ),
                        "seed": seed,
                        "lang": language,
                        "max_cost_eur": str(budget - retry_cost),
                        "timeout": remaining,
                    },
                    remaining,
                )
                break
            except GatewayError as exc:
                spent = Decimal(str(exc.measurements.get("reserved_eur", 0)))
                retry_cost += min(budget - retry_cost, spent)
                remaining = operation_timeout - (
                    time.monotonic() - started - item.startup_seconds
                )
                if (
                    exc.code != "gpu_busy"
                    or attempt
                    or remaining <= 1
                    or retry_cost >= budget
                ):
                    raise
                item.provider_retries += 1
                await asyncio.sleep(min(1, remaining))
        image = ImageURL.model_validate({"url": response["image"]})
        cost = float(Decimal(str(response["cost_eur"])) + retry_cost)
        item.provider_retries += int(str(response.get("provider_retries", 0)))
        if not 0 <= Decimal(str(cost)) <= budget:
            raise ValueError("invalid_provider_cost")
        labels = json.loads(Path("prompts/image-labels.json").read_text())
        label = labels[language]
        reference = store(image.url)
        item.images = [
            {
                k: v
                for k, v in response.items()
                if k != "image" and isinstance(v, (str, int))
            }
        ]
        item.images[0]["reference"] = reference
        item.images[0]["original_request"] = item.question
        item.reponse = f"![{label}]({reference})"
        from services.orchestrator.image_verification import verification_note

        note = verification_note(response.get("verification"), language)
        if note:
            item.reponse += "\n\n" + note
        item.cout_eur, item.state = cost, "done"
        sink = sink_context.get()
        if sink is not None:
            await sink({"delta": {"content": item.reponse}})
    except GatewayError as exc:
        if not generating and exc.code in {"configuration_missing", "cost_budget"}:
            if exc.code == "configuration_missing":
                item.cout_eur = 0.0
            await image_progress(
                "failed",
                failure_message(language, exc.code, exc.status, exc.measurements),
            )
            raise
        await finish_failure(item, language, exc)
    except TimeoutError:
        elapsed = time.monotonic() - started - item.startup_seconds
        await finish_failure(
            item,
            language,
            GatewayError(
                "timeout",
                504,
                measurements={
                    "elapsed_seconds": round(elapsed, 3),
                    "limit_seconds": operation_timeout,
                },
            ),
        )
    finally:
        item.latence_ms["generation"] = (time.monotonic() - started) * 1000


async def image_progress(step: str, message: str = "") -> None:
    if sink := sink_context.get():
        await sink(
            {
                "phase": "image_progress",
                "step": step,
                "message": message,
                "width": 1024,
                "height": 1024,
            }
        )


async def finish_failure(item: Interaction, language: str, error: GatewayError) -> None:
    item.reponse = failure_message(
        language, error.code, error.status, error.measurements
    )
    item.state = "done"
    item.erreurs.append("image_" + error.code)
    item.rejection = {"code": "image_" + error.code, "message": item.reponse}
    await image_progress("failed", item.reponse)
    if sink := sink_context.get():
        await sink({"delta": {"content": item.reponse}})
