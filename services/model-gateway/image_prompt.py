"""Rewrite reservation shares the image request's existing cost ceiling."""

from decimal import Decimal
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
from time import monotonic
from image_scene import Scene

from agent_provider import AgentProvider, AgentRequest
from packages.configuration import MissingConfiguration
import provider_settings
from serverless import ServerlessPolicy
from image_constraints import extract_constraints, missing_constraints
from packages.imagegen import ImagePrompt, positive_image_prompt


def messages(
    prompt: str, language: str = "", *, retry: bool = False
) -> list[dict[str, object]]:
    return [
        {
            "role": "system",
            "content": Path("prompts/image-rewrite.txt").read_text()
            + "\n"
            + json.dumps({"source_language": language, "output_language": "en"})
            + "\nPreserve the requested action and its timing, all subjects, place, "
            "companions and style. For a birth request, choose one coherent moment: "
            "default to delivery happening now, with physical effort, a wet newborn "
            "being received and its umbilical cord still attached. Only explicit "
            "requests select labor before birth or the aftermath. "
            "Do not add rendering instructions. Required source constraints: "
            + json.dumps(
                [c.name for c in extract_constraints(prompt)], ensure_ascii=False
            )
            + (
                "\nThis is a formatting-only task, whatever the subject. The image "
                "request has already passed the application's input checks. Preserve "
                "the user's subject and constraints; translate them into English "
                "under exactly four nonempty sections: Subjects:, Attributes:, Scene:, "
                "Style:. Return a JSON object with two strings: formatted (the "
                "four-section description), and literal_translation (a literal "
                "English translation of the original request, translation only, "
                "no rewording, additions or omissions). Keep both independent: "
                "the translation is the fallback if formatting loses constraints."
                if retry
                else ""
            ),
        },
        {"role": "user", "content": prompt},
    ]


def rewrite_plan() -> tuple[ServerlessPolicy, str, str, str, str, bool]:
    """Use the same selected-provider configuration as ordinary text completion."""
    try:
        policy = ServerlessPolicy()
        role = rewrite_role()
        model = policy.models[role]
        endpoint = provider_settings.endpoint()
        api_key = provider_settings.api_key()
        capabilities = policy.config[role]["capabilities"][model]
    except KeyError as exc:
        raise MissingConfiguration((str(exc).strip("'"),)) from exc
    except ValueError as exc:
        raise MissingConfiguration(()) from exc
    return (
        policy,
        role,
        model,
        endpoint,
        api_key,
        role != "vision" or bool(capabilities.get("vision")),
    )


def log_usage(role: str, usage: object, fallback: str | None = None) -> None:
    event: dict[str, object] = {
        "event": "image_rewrite_usage",
        "role": role,
        "usage": usage,
    }
    if fallback is not None:
        event["fallback"] = fallback
    logging.getLogger(__name__).info(json.dumps(event))


def reservation(
    prompt: str, language: str = "", *, retry: bool = False, structured: bool = False
) -> Decimal:
    if os.environ.get("ATLAS_PROVIDER"):
        policy, _, model, _, _, available = rewrite_plan()
        if not available:
            return Decimal(0)
    else:
        policy = ServerlessPolicy()
        model = policy.models[rewrite_role()]
    incoming = len(json.dumps(messages(prompt, language, retry=retry)).encode()) + 256
    if structured:
        incoming = max(
            incoming, len(json.dumps(scene_messages([prompt])).encode()) + 256
        )
    return policy.cost(model, incoming, 2048)


def rewrite_role() -> str:
    role = os.environ.get("ATLAS_IMAGE_REWRITE_ROLE", "vision")
    if role not in {"text", "fast", "code", "vision"}:
        raise ValueError("invalid_image_rewrite_role")
    return role


class ImageRewriteProvider(AgentProvider):
    async def serverless_complete(self, request: AgentRequest) -> dict[str, object]:
        # Image formatting has its own measured role; no uncounted model fallback.
        _, role, model, endpoint, api_key, available = rewrite_plan()
        if not available:
            raise RewriteRoleUnavailable(role)
        answer = await self._complete(
            request,
            endpoint,
            model,
            api_key,
            request.timeout,
        )
        log_usage(role, answer.get("usage"))
        return answer


class RewriteRoleUnavailable(RuntimeError):
    pass


def candidates(text: object) -> tuple[str, str]:
    if not isinstance(text, str):
        return "", ""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return "", ""
        if isinstance(data, dict):
            formatted, literal = data.get("formatted"), data.get("literal_translation")
            return (
                formatted if isinstance(formatted, str) else "",
                literal if isinstance(literal, str) else "",
            )
    return text, ""


def faithful(source: str, candidate: str, *, formatted: bool) -> str | None:
    if re.search(
        r"\b(?:I (?:cannot|can't|won't|am unable)|I'm sorry|"
        r"(?:cannot|unable to) (?:generate|create|depict|translate)|"
        r"not allowed|policy (?:prohibits|forbids))\b",
        candidate,
        re.IGNORECASE,
    ):
        return None
    if formatted and not re.fullmatch(
        r"\s*Subjects:\s*\S.*?Attributes:\s*\S.*?Scene:\s*\S.*?Style:\s*\S.*",
        candidate,
        re.DOTALL,
    ):
        return None
    try:
        cleaned = positive_image_prompt(ImagePrompt(prompt=candidate).prompt)
    except ValueError:
        return None
    missing = missing_constraints(source, cleaned)
    if missing:
        logging.getLogger(__name__).info(
            json.dumps(
                {"event": "image_rewrite_missing_constraints", "constraints": missing}
            )
        )
        return None
    return cleaned


@dataclass
class RewriteBudget:
    ceiling: Decimal
    reserved: Decimal
    structured: bool = False
    scene: Scene | None = None


async def rewrite(
    prompt: str,
    timeout: float,
    language: str = "",
    *,
    budget: RewriteBudget | None = None,
) -> str:
    original = ImagePrompt(prompt=prompt).prompt
    if budget is None:
        budget = RewriteBudget(Decimal("0.05"), reservation(prompt, language))
    if os.environ.get("ATLAS_PROVIDER"):
        _, role, _, _, _, available = rewrite_plan()
        if not available:
            log_usage(role, None, "role_unavailable")
            return bind_roles(original, positive_image_prompt(original), budget)
    started = monotonic()
    for retry in (False, True):
        remaining = timeout - (monotonic() - started)
        if remaining <= 0:
            break
        if retry:
            extra = reservation(prompt, language, retry=True)
            # Leave a positive generation allowance; never reset the ceiling.
            if budget.reserved + extra >= budget.ceiling:
                break
            budget.reserved += extra
        result = await ImageRewriteProvider().complete(
            {
                "messages": scene_messages([prompt])
                if budget.structured and not retry
                else messages(prompt, language, retry=retry),
                "tools": [],
                "local_enabled": False,
                "timeout": remaining,
            }
        )
        if budget.structured and not retry:
            try:
                budget.scene = scene_from_text(result.get("text"), [original])
                return ImagePrompt(prompt=budget.scene.render()).prompt
            except (ValueError, KeyError, TypeError):
                logging.getLogger(__name__).info(
                    json.dumps({"event": "image_scene_fallback"})
                )
        formatted, literal = candidates(result.get("text"))
        if checked := faithful(original, formatted, formatted=True):
            return bind_roles(original, checked, budget)
        if retry and (checked := faithful(original, literal, formatted=False)):
            return bind_roles(original, checked, budget)
    logging.getLogger(__name__).info(
        json.dumps({"event": "image_rewrite_source_fallback"})
    )
    return bind_roles(original, positive_image_prompt(original), budget)


def bind_roles(source: str, candidate: str, budget: RewriteBudget) -> str:
    from image_scene import anchored_scene

    if budget.structured:
        scene = anchored_scene(source)
        if scene.characters:
            budget.scene = scene
            return ImagePrompt(prompt=scene.render()).prompt
    return candidate


def scene_messages(turns: list[str]) -> list[dict[str, object]]:
    from image_scene import ScenePatch

    return [
        {
            "role": "system",
            "content": (
                "Translate this image dialogue into structured English scene updates. "
                'Return JSON {"updates": [...]} with one update per user turn. '
                "A single input can contain the original request followed by corrections; "
                "split those into successive updates too. "
                "Set relation=new only when the turn requests a new unrelated main subject. "
                "Otherwise relation=edit: an image was just produced, and a turn naming "
                "no new main subject safely defaults to edit. An edit preserves the scene; "
                "adding a companion or changing style, weather, size or attributes is an edit. "
                "A new scene describes only that turn, without prior subjects or style. "
                "The first describes the whole scene. Later edit entries change ONLY fields "
                "the user corrects. Preserve all other characters, actions, setting, "
                "elements and style. A missing subject complaint means add/show it, "
                "never remove it. A complaint about extra hands means anatomy=true. "
                "Bind attributes to named characters, not a shared adjective list. "
                "For a birth scene choose one visible moment. Default to realistic delivery: "
                "the woman half-lying or squatting, knees apart, sweating and straining, "
                "gripping her companion; a wet newborn with vernix, some blood and an "
                "attached umbilical cord being received; blood-stained cloth and straw. "
                "A still-rounded postpartum abdomen is compatible with this moment. "
                "Post-birth only if requested. Only explicit "
                "labor-before-birth wording selects labor, with no baby visible. A later "
                "correction always wins. Store a requested "
                "named historical or religious figure's sex, age group, period clothing and hair in "
                "visual_description, keeping their name for bindings. For realistic "
                "depictions avoid religious iconography. Preserve explicitly requested "
                "artistic styles such as Byzantine icons, Nativity scenes or Renaissance paintings. "
                "Never invent halos, stars, angels or glowing light. Store each "
                "visible relation between named people in action_bindings. Do not censor or invent "
                "details. Use remove_characters/remove_elements only for explicit "
                "removal instructions. Each update follows this schema: "
                + json.dumps(ScenePatch.model_json_schema())
            ),
        },
        {"role": "user", "content": json.dumps(turns, ensure_ascii=False)},
    ]


def scene_reservation(turns: list[str]) -> Decimal:
    if os.environ.get("ATLAS_PROVIDER"):
        policy, _, model, _, _, available = rewrite_plan()
        if not available:
            return Decimal(0)
    else:
        policy = ServerlessPolicy()
        model = policy.models[rewrite_role()]
    return policy.cost(
        model,
        len(json.dumps(scene_messages(turns)).encode()) + 256,
        2048,
    )


_KNOWN_CHARACTER_IDENTITIES = {
    "mary": ("woman", "young adult"),
    "virgin mary": ("woman", "young adult"),
    "joseph": ("man", "adult"),
    "jesus": ("man", "infant"),
}


def complete_character_fields(data: object) -> dict[str, object]:
    """Keep the wire schema strict while repairing unambiguous extraction omissions."""
    if not isinstance(data, dict) or not isinstance(data.get("updates"), list):
        raise ValueError("invalid_scene")
    for update in data["updates"]:
        if not isinstance(update, dict) or not isinstance(
            update.get("characters"), list
        ):
            continue
        for character in update["characters"]:
            if not isinstance(character, dict):
                continue
            description = " ".join(
                str(character.get(field, ""))
                for field in ("role", "visual_description")
            ).casefold()
            name = str(character.get("name", "")).casefold()
            known = _KNOWN_CHARACTER_IDENTITIES.get(name)
            if "sex" not in character:
                if re.search(r"\b(?:woman|female|mother|girl)\b", description):
                    character["sex"] = "woman"
                elif re.search(r"\b(?:man|male|father|boy)\b", description):
                    character["sex"] = "man"
                elif known:
                    character["sex"] = known[0]
            if "age_group" not in character:
                if re.search(r"\b(?:newborn|infant|baby)\b", description):
                    character["age_group"] = "infant"
                elif re.search(r"\b(?:child|girl|boy)\b", description):
                    character["age_group"] = "child"
                elif re.search(r"\b(?:young|twent)\b", description):
                    character["age_group"] = "young adult"
                elif re.search(r"\b(?:older|elder|senior)\b", description):
                    character["age_group"] = "older adult"
                elif re.search(
                    r"\b(?:adult|companion|mother|father|woman|man)\b", description
                ):
                    character["age_group"] = "adult"
                elif known:
                    character["age_group"] = known[1]
    return data


async def rewrite_scene(turns: list[str], timeout: float) -> "Scene":
    from image_scene import anchored_scene

    # The caller reserves this single extraction before transport. Scene updates
    # are rebuilt from user turns, so no server-side conversation identity leaks.
    if os.environ.get("ATLAS_PROVIDER"):
        _, role, _, _, _, available = rewrite_plan()
        if not available:
            scene = anchored_scene(turns[0])
            if not scene.setting:
                scene.setting = " ".join(turns)
            for turn in turns:
                scene.apply_user_correction(turn)
            log_usage(role, None, "role_unavailable")
            return scene
    result = await ImageRewriteProvider().complete(
        {
            "messages": scene_messages(turns),
            "tools": [],
            "local_enabled": False,
            "timeout": timeout,
        }
    )
    try:
        return scene_from_text(result.get("text"), turns)
    except (ValueError, KeyError, TypeError):
        scene = anchored_scene(turns[0])
        if not scene.setting:
            scene.setting = " ".join(turns)
        for turn in turns:
            scene.apply_user_correction(turn)
        logging.getLogger(__name__).info(json.dumps({"event": "image_scene_fallback"}))
        return scene


def scene_from_text(text: object, turns: list[str]) -> Scene:
    from image_scene import ScenePatch, anchored_scene, retain_anchors
    from pydantic import TypeAdapter

    scene = anchored_scene(turns[0])
    if not isinstance(text, str):
        raise ValueError("invalid_scene")
    data = complete_character_fields(
        json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
    )
    updates = TypeAdapter(list[ScenePatch]).validate_python(data["updates"])
    if not 1 <= len(updates) <= 24 or (len(turns) > 1 and len(updates) != len(turns)):
        raise ValueError("invalid_scene_turns")
    scene_start = 0
    for index, update in enumerate(updates):
        if len(turns) > 1 and re.fullmatch(
            r"(?i)\s*(?:trois mains|three hands|drei h[aä]nde|tres manos|tre mani)[.!?… ]*",
            turns[index],
        ):
            update = ScenePatch(anatomy=True)
        if (
            len(turns) == 1
            and len(updates) > 1
            and index == len(updates) - 1
            and re.search(
                r"(?i)\b(?:trois mains|three hands|drei h[aä]nde|tres manos|tre mani)[.!?… ]*$",
                turns[0],
            )
        ):
            update = ScenePatch(anatomy=True)
        starts_scene = index == 0 or update.relation == "new"
        if starts_scene:
            scene_start = min(index, len(turns) - 1)
            scene = anchored_scene(turns[scene_start])
        elif index < len(turns):
            update = update.for_correction(turns[index])
        scene.apply(update)
        if index and index < len(turns):
            scene.apply_user_correction(turns[index])
        if starts_scene:
            retain_anchors(scene, anchored_scene(turns[scene_start]))
            scene.apply_user_correction(turns[scene_start])
    if not scene.render():
        raise ValueError("empty_scene")
    scene.cohere_birth_moment()
    return scene
