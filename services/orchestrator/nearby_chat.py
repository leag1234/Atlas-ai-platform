"""Location-bound tools and a checked nearby answer, shared across chat surfaces."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import re
from typing import Any
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.location import LEDGER_LABEL, Location
from services.orchestrator.loop import Call, Message, Reservation
from services.orchestrator.nearby import NearbyError, OSM, rank
from services.orchestrator.stream_client import sink_context
from services.orchestrator.tools import Runtime, Search


class NearbyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str = Field(min_length=1, max_length=1000)
    radius: int = Field(default=1500, ge=100, le=10000)
    open_now: bool = True


class GeocodeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    place: str = Field(min_length=2, max_length=200)


def requested_radius(text: str) -> int:
    match = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(km|kilomet(?:er|re)s?|m|met(?:er|re)s?)\b", text, re.I
    )
    if not match:
        return 1500
    amount = float(match[1].replace(",", "."))
    return round(amount * (1000 if match[2].lower().startswith("k") else 1))


def declarations() -> list[Message]:
    return [
        dict(
            type="function",
            function=dict(
                name=name,
                description=description,
                parameters=schema.model_json_schema(),
            ),
        )
        for name, description, schema in (
            (
                "nearby_places",
                "Find nearby shops using only the approximate location explicitly consented for this request. Category or free text. Returns verified local schedule calculations and straight-line distances; unknown hours remain unknown.",
                NearbyArguments,
            ),
            (
                "geocode",
                "Resolve only the place explicitly supplied by the user for this request. No default city; no position is returned to the model.",
                GeocodeArguments,
            ),
        )
    ]


class NearbyTools:
    def __init__(
        self,
        location: Location,
        search: Runtime,
        osm: OSM | None = None,
        now: datetime | None = None,
        language: str = "en",
        max_calls: int = 8,
        cost_limit: Decimal = Decimal("0.05"),
        check: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.location, self.search = location, search
        self.osm = osm or OSM()
        self.now = now or datetime.now(timezone.utc)
        self.position = location.position
        self.area = "approximate area"
        self.calls = 0
        self.result: dict[str, Any] | None = None
        self.result_key: tuple[str, int, bool] | None = None
        self.error = ""
        self.cost = Decimal(0)
        self.language, self.max_calls, self.cost_limit = language, max_calls, cost_limit
        self.check = check

    def estimate(self, call: Call) -> Reservation:
        # The nested verification search has its own provider reservation.
        return self.search.estimate(Call(call.id, "web_search", "{}"))

    def step(self) -> None:
        if self.calls >= self.max_calls:
            raise NearbyError("nearby_tool_limit")
        self.calls += 1

    async def resolve(self) -> None:
        if self.position is None and self.location.place:
            self.step()
            self.position, self.area = await self.osm.geocode(self.location.place)
        if self.position is None:
            raise NearbyError("location_required")

    async def execute(self, call: Call, timeout: float) -> Message:
        try:
            async with asyncio.timeout(timeout):
                if self.check:
                    await self.check()
                self.step()
                if call.name == "geocode":
                    args = GeocodeArguments.model_validate_json(call.arguments)
                    if args.place != self.location.place:
                        raise NearbyError("explicit_place_required")
                    await self.resolve()
                    return dict(area=self.area, location=LEDGER_LABEL)
                if call.name != "nearby_places":
                    raise NearbyError("tool_unavailable")
                request = NearbyArguments.model_validate_json(call.arguments)
                result_key = (request.category, request.radius, request.open_now)
                if self.result is not None and self.result_key == result_key:
                    return dict(trust="untrusted", data=self.result)
                await self.resolve()
                assert self.position is not None
                candidates = await self.osm.candidates(
                    self.position,
                    request.category,
                    request.radius,
                    self.now,
                    self.language,
                )
                selected = rank(candidates, request.open_now)
                warnings = list(self.osm.failures)
                web_fallback: Message = {}
                if not selected:
                    # A public locality is sufficient for fallback; never put a
                    # browser fix or a typed street/address in a search query.
                    area = self.osm.area or (
                        self.area if self.area != "approximate area" else ""
                    )
                    if area:
                        web_fallback = await self.search_public(
                            f"{request.category} {area} opening hours", timeout
                        )
                    else:
                        warnings.append("public_locality_required_for_web")
                # Search public business names/addresses only. Never send origin,
                # typed location, browser metadata or conversation to search.
                uncertain = [
                    p
                    for p in selected
                    if p["opening"]["status"] == "unknown"
                    or "PH" in (p["opening_hours"] or "")
                ]
                checks = uncertain[:2] or selected[:1]
                for place in checks:
                    query = f"{place['name']} {place['address']} opening hours temporary closure {self.now.date().isoformat()}"
                    evidence = await self.search_public(query, timeout)
                    if evidence.get("error"):
                        warnings.append("web_verification_unavailable")
                    place["web_check"] = evidence
                    # Conflicting closure evidence downgrades confidence. Prose
                    # cannot establish a new schedule or a guaranteed opening.
                    data = evidence.get("data", evidence)
                    if isinstance(data, dict):
                        snippets = " ".join(
                            str(r.get("snippet", ""))
                            for r in data.get("results", [])
                            if isinstance(r, dict)
                        ).lower()
                        if any(
                            term in snippets
                            for term in (
                                "temporarily closed",
                                "permanently closed",
                                "fermeture temporaire",
                                "fermé temporairement",
                                "fermeture définitive",
                                "holiday hours",
                                "horaires exceptionnels",
                            )
                        ):
                            place["opening"] = dict(
                                status="unknown",
                                closing_time=None,
                                next_opening=None,
                                closing_soon=False,
                                timezone=place["opening"].get("timezone"),
                            )
                            warnings.append("web_schedule_conflict")
                self.result = dict(
                    places=selected,
                    source=self.osm.served_by if selected else "web_search",
                    web_fallback=web_fallback,
                    open_count=sum(
                        p["opening"]["status"] == "open" for p in candidates
                    ),
                    radius_m=request.radius,
                    evaluated_at=self.now.isoformat(),
                    location=LEDGER_LABEL,
                    area=self.area,
                    warnings=warnings,
                )
                self.result_key = result_key
                return dict(trust="untrusted", data=self.result)
        except ValidationError:
            self.error = "invalid_nearby_arguments"
        except (NearbyError, TimeoutError) as exc:
            self.error = str(exc) if isinstance(exc, NearbyError) else "nearby_timeout"
        return {"error": self.error}

    async def search_public(self, query: str, timeout: float) -> Message:
        if self.check:
            await self.check()
        self.step()
        args = Search.model_validate(dict(query=query[:1000], n=3, lang=self.language))
        call = Call("nearby-check", "web_search", args.model_dump_json())
        reservation = self.search.estimate(call)
        if self.cost + reservation.cost > self.cost_limit:
            return {"error": "nearby_cost_limit"}
        self.cost += reservation.cost
        return await self.search.execute(call, min(timeout, 20))


def safe_text(value: Any) -> str:
    # Provider strings are data, never Markdown/HTML instructions or links.
    import re

    return re.sub(r"[\[\]<>`*\n\r]", "", str(value))[:400]


def render(result: dict[str, Any], language: str) -> str:
    french = language == "fr"
    places = result["places"]
    if not places:
        check = result.get("web_fallback", {})
        data = check.get("data", check)
        rows = data.get("results", []) if isinstance(data, dict) else []
        lines = [
            "Aucun lieu OSM trouvé ; résultats web à confirmer, distances et horaires inconnus."
            if french
            else "No OSM places found; web results need confirmation, distances and hours are unknown."
        ]
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("link", ""))
            if url.startswith("https://") and not any(c in url for c in "\n\r()<> "):
                lines.append(
                    f"- **{safe_text(row.get('title', ''))}** — {safe_text(row.get('snippet', ''))} [Source]({url})"
                )
        if not rows:
            lines.append(
                "Indiquez une commune ou essayez un rayon plus large."
                if french
                else "Enter a town or try a larger radius."
            )
        return (
            "\n\n".join(lines)
            + "\n\n© OpenStreetMap contributors (ODbL). Source: "
            + safe_text(result.get("source", "unknown"))
        )
    lines = []
    if result["open_count"] < 3:
        lines.append(
            (
                f"{result['open_count']} lieu(x) indiqué(s) ouvert(s) dans ce rayon ; les prochaines ouvertures et horaires inconnus suivent."
                if french
                else f"{result['open_count']} place(s) listed open in this radius; next openings and unknown hours follow."
            )
        )
    lines.append(
        "Distances approximatives à vol d’oiseau ; horaires locaux selon OpenStreetMap."
        if french
        else "Approximate straight-line distances; local hours according to OpenStreetMap."
    )
    for place in places:
        hours = place["opening"]
        state = {
            "open": "Ouvert" if french else "Open",
            "closed": "Fermé" if french else "Closed",
            "unknown": "Horaires non renseignés" if french else "Hours not listed",
        }[hours["status"]]
        if hours["closing_time"]:
            state += (", ferme à " if french else ", closes at ") + hours[
                "closing_time"
            ]
        elif hours["status"] == "open":
            state += (
                ", fermeture non indiquée" if french else ", no closing time listed"
            )
        if hours["closing_soon"]:
            state += " (ferme bientôt)" if french else " (closing soon)"
        if hours["next_opening"]:
            state += (", prochaine ouverture " if french else ", next opens ") + hours[
                "next_opening"
            ]
        address = safe_text(place["address"]) or (
            "Adresse non renseignée" if french else "Address not listed"
        )
        lines.append(
            f"- **{safe_text(place['name'])}** — ≈ {place['distance_m']} m — {state}. {address}. [OpenStreetMap]({place['source']})"
        )
        # Search evidence supplements OSM; do not convert prose into invented
        # opening times or claim a search snippet confirms a current opening.
        check = place.get("web_check", {})
        data = check.get("data", check)
        results = data.get("results", []) if isinstance(data, dict) else []
        if isinstance(results, list):
            for source in results[:1]:
                if isinstance(source, dict) and str(source.get("link", "")).startswith(
                    "https://"
                ):
                    url = str(source["link"])
                    if not any(c in url for c in "\n\r()<> "):
                        lines.append(
                            f"  {'Vérification web (à confirmer)' if french else 'Web check (not a live opening guarantee)'}: {safe_text(source.get('snippet', ''))} [Source]({url})"
                        )
        if place["phone"]:
            lines.append(
                ("  Téléphone : " if french else "  Phone: ")
                + safe_text(place["phone"])
            )
    if result["warnings"]:
        lines.append(
            "Certains résultats ou horaires restent incertains ; confirmez par téléphone."
            if french
            else "Some results or hours remain uncertain; confirm by phone."
        )
    lines.append(
        "\n© OpenStreetMap contributors (ODbL). Source: "
        + safe_text(result.get("source", "OpenStreetMap"))
        + ". "
        + LEDGER_LABEL
    )
    return "\n\n".join(lines)


async def _process_nearby(
    request: ChatRequest,
    item: Interaction,
    *,
    max_calls: int = 8,
    cost_limit: Decimal = Decimal("0.05"),
    check: Callable[[], Awaitable[None]] | None = None,
) -> int:
    sink = sink_context.get()
    location = request.location
    if location is None or (location.position is None and not location.place):
        item.reponse = (
            "Indiquez un lieu, par exemple « près de la place Bellecour », ou partagez votre position approximative pour cette demande."
            if request.lang == "fr"
            else "Enter a place, for example ‘near Bellecour square’, or share your approximate location for this request."
        )
        item.state, item.task_type, item.cout_eur = "done", "nearby", 0.0
        if sink:
            await sink(
                {"phase": "location_required", "delta": {"content": item.reponse}}
            )
        return 0
    from services.orchestrator.chat_pipeline import ChatTools

    search = ChatTools(item, "")
    search.daily_ledger = getattr(request, "daily_ledger", None)
    search.daily_user = getattr(request, "daily_user", "unknown")
    search.daily_lang = request.lang
    tools = NearbyTools(
        location,
        search,
        language=request.lang,
        max_calls=max_calls,
        cost_limit=cost_limit,
        check=check,
    )
    await tools.execute(
        Call(
            "nearby-prefetch",
            "nearby_places",
            json.dumps(
                dict(
                    category=request.user_text[:1000],
                    radius=requested_radius(request.user_text),
                )
            ),
        ),
        90,
    )
    if tools.result:
        item.retrieved_urls.extend(p["source"] for p in tools.result["places"])
        item.location_used = LEDGER_LABEL
        item.nearby_source = tools.result["source"]
    # All user-visible facts are already evaluated by the location-bound tool.
    # Work uses these same tools through its normal model discovery; ordinary
    # chat does not need an extra model call whose prose would be discarded.
    item.reponse = (
        render(tools.result, request.lang)
        if tools.result
        else (
            "Recherche de proximité indisponible : "
            if request.lang == "fr"
            else "Nearby search unavailable: "
        )
        + (tools.error or "nearby_unavailable")
    )
    item.state = "done" if tools.result else "failed"
    item.task_type, item.cout_eur = "nearby", float(tools.cost)
    if tools.error:
        item.erreurs.append(tools.error)
    if sink:
        if tools.result:
            await sink({"phase": "location_used", "description": LEDGER_LABEL})
        await sink({"delta": {"content": item.reponse}})
    return tools.calls


async def process_nearby(
    request: ChatRequest,
    item: Interaction,
    *,
    max_calls: int = 8,
    cost_limit: Decimal = Decimal("0.05"),
    check: Callable[[], Awaitable[None]] | None = None,
) -> int:
    location = request.location
    try:
        return await _process_nearby(
            request,
            item,
            max_calls=max_calls,
            cost_limit=cost_limit,
            check=check,
        )
    finally:
        request.location = None
        if location:
            location.position = None
            location.place = None
