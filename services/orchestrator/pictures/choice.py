"""Choosing one of the candidates: a short answer in the chat, or a click.

A chat answer such as "la deuxième", "the second", "2" or "je prends la 3",
right after a picture answer, makes that candidate the conversation's
picture: a new version of the drawing artifact holds it, so the next change
("make the fox bigger") applies to it through the ordinary modify path. A
click on a number (routes.py) records the choice; the next turn writes it
into the artifact before anything else reads it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from packages.artifact_source import Block, prepare
from services.orchestrator.pictures import store
from services.orchestrator.pictures.texts import text

LOGGER = logging.getLogger(__name__)
SET_LINK = re.compile(r"/images/picture/([0-9a-f]{32})/[1-4]\b")
MAX_CHOICE_CHARS = 60
ORDINALS: dict[int, tuple[str, ...]] = {
    1: ("first", "premier", "premiere", "erste", "ersten", "erstes", "erster",
        "primero", "primera", "prima", "primo"),
    2: ("second", "deuxieme", "seconde", "zweite", "zweiten", "zweites", "zweiter",
        "segundo", "segunda", "seconda", "secondo", "two", "deux", "zwei", "dos",
        "due"),
    3: ("third", "troisieme", "dritte", "dritten", "drittes", "dritter", "tercero",
        "tercera", "terza", "terzo", "three", "trois", "drei", "tres", "tre"),
    4: ("fourth", "quatrieme", "vierte", "vierten", "viertes", "vierter", "cuarto",
        "cuarta", "quarta", "quarto", "four", "quatre", "vier", "cuatro",
        "quattro"),
}  # fmt: skip
NUMBER = re.compile(r"^#?n?o?([1-4])(?:e|eme|er|re|nd|rd|th|st|o|a)?$")
# Words a short choice may hold besides its number.
FILLER = frozenset(
    """
    the a la le l les une un one celle celui ci la-bas cette ce c est cest
    je j prends prend prendre choisis choisir choisi choose chosen take pick
    i ill want veux voudrais would like prefer prefere aime love
    picture image images img bild bilder imagen imagenes immagine dessin
    drawing proposal proposition propuesta proposta vorschlag option version
    number numero nummer no nr n num
    please svp stp merci thanks thank you bitte danke por favor gracias per
    favore grazie ok okay oui yes ja si va pour go with for
    ich nehme das die der den el lo il me quedo con quiero voglio prendo
    scelgo elijo wahle mit de du des
    """.split()
)


def _words(message: str) -> list[str]:
    folded = (
        unicodedata.normalize("NFKD", message)
        .encode("ascii", "ignore")
        .decode()
        .casefold()
    )
    return re.findall(r"[#a-z0-9]+", folded.replace("'", " ").replace("°", "o"))


def choice_number(message: str) -> int | None:
    """The candidate number (1 to 4) a short choice names, or None."""
    if not message.strip() or len(message.strip()) > MAX_CHOICE_CHARS:
        return None
    found: set[int] = set()
    for word in _words(message):
        number = NUMBER.match(word)
        if number:
            found.add(int(number.group(1)))
            continue
        ordinal = next((n for n, names in ORDINALS.items() if word in names), None)
        if ordinal is not None:
            found.add(ordinal)
        elif word not in FILLER:
            return None
    return found.pop() if len(found) == 1 else None


def latest_set_id(messages: Sequence[Mapping[str, object]]) -> str | None:
    """The picture set of the last assistant answer, when it is one."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            return None
        found = SET_LINK.findall(content)
        return found[-1] if found else None
    return None


def _owned(picture_set: store.PictureSet, owner: str) -> bool:
    return picture_set.owner == store.owner_hash(owner)


def _write_choice(
    picture_set: store.PictureSet, index: int, owner: str, request_id: str
) -> tuple[str, int, str] | None:
    """Store the chosen candidate as the next artifact version; (id, version,
    filename) or None when the set has no artifact or the store refused."""
    from services.orchestrator import artifact_store
    from services.orchestrator.artifact_store import ArtifactError

    if not picture_set.artifact_id:
        return None
    prepared = prepare(
        Block("svg", picture_set.candidates[index].svg, True),
        picture_set.title,
        picture_set.language,
    )
    if prepared.problems:
        return None
    try:
        current = artifact_store.read(
            picture_set.artifact_id, owner, picture_set.artifact_version
        )
        stored = artifact_store.update(
            picture_set.artifact_id,
            owner,
            picture_set.title,
            current.filename,
            prepared.document.encode("utf-8"),
            request_id,
            picture_set.artifact_version,
        )
    except ArtifactError as error:
        LOGGER.warning("picture_choice_store_failed code=%s", error.code)
        return None
    picture_set.artifact_version = int(stored["version"])
    picture_set.artifact_candidate = index
    picture_set.current_source = picture_set.candidates[index].source
    picture_set.seed = picture_set.candidates[index].seed
    picture_set.prompt = picture_set.candidates[index].prompt or picture_set.prompt
    return str(stored["id"]), int(stored["version"]), current.filename


def _artifact_link(
    picture_set: store.PictureSet, owner: str, language: str
) -> tuple[str, dict[str, int | str]] | None:
    """The footer of the set's artifact version (and its record), or None."""
    from services.orchestrator import artifact_store
    from services.orchestrator.artifact_store import ArtifactError
    from services.orchestrator.artifact_tools import footer

    if not picture_set.artifact_id:
        return None
    try:
        stored = artifact_store.read(
            picture_set.artifact_id, owner, picture_set.artifact_version
        )
    except ArtifactError:
        return None
    link = footer(
        picture_set.artifact_id,
        picture_set.artifact_version,
        picture_set.title,
        stored.filename,
        language,
        temporary=False,
    )
    return link, {
        "id": picture_set.artifact_id,
        "version": picture_set.artifact_version,
    }


def _apply(
    picture_set: store.PictureSet, index: int, owner: str, request_id: str
) -> None:
    """Make candidate `index` the set's choice and, when needed, its artifact."""
    if index != picture_set.artifact_candidate:
        _write_choice(picture_set, index, owner, request_id)
    picture_set.chosen = index
    store.update(picture_set)


async def handle_choice(request: Any, item: Any) -> bool:
    """Answer a choice right after a picture answer; False when it is not one."""
    import asyncio

    from services.orchestrator import image_store

    history = [{"role": m.role, "content": m.text} for m in request.messages[:-1]]
    set_id = latest_set_id(history)
    number = choice_number(request.user_text) if set_id else None
    if set_id is None or number is None:
        return False
    owner = str(getattr(request, "artifact_owner", "") or "")
    picture_set = await asyncio.to_thread(store.load, set_id)
    language = request.lang if request.lang in ("en", "fr", "de", "es", "it") else "en"
    if picture_set is not None and not _owned(picture_set, owner):
        return False
    item.task_type = "drawing"
    if picture_set is None:
        item.reponse, item.state = text(language, "expired"), "done"
        return True
    count = len(picture_set.candidates)
    if number > count:
        item.reponse = text(language, "missing", count=count, n=number)
        item.state = "done"
        return True
    index = number - 1
    await asyncio.to_thread(_apply, picture_set, index, owner, item.request_id)
    image = image_store.reference(picture_set.candidates[index].image_key)
    lines = [text(language, "chosen", n=number), f"![{number}]({image})"]
    item.images = [{"reference": image, "kind": "picture"}]
    link = await asyncio.to_thread(_artifact_link, picture_set, owner, language)
    if link is not None:
        lines.append(link[0])
        item.artifacts = [link[1]]
    item.reponse, item.state = "\n\n".join(lines), "done"
    return True


def current_after_click(request: Any, current: Any, request_id: str) -> Any:
    """The conversation's current artifact once a clicked choice is written.

    When the last answer is a picture set whose clicked candidate is not the
    one its artifact holds, the clicked one becomes the next artifact version
    and is returned as the current artifact; otherwise `current` unchanged.
    """
    from services.orchestrator.artifact_tools import current_artifact_message

    history = [{"role": m.role, "content": m.text} for m in request.messages[:-1]]
    set_id = latest_set_id(history)
    if set_id is None:
        return current
    picture_set = store.load(set_id)
    owner = str(getattr(request, "artifact_owner", "") or "")
    if (
        picture_set is None
        or not _owned(picture_set, owner)
        or picture_set.chosen == picture_set.artifact_candidate
    ):
        return current
    before = picture_set.artifact_version
    _apply(picture_set, picture_set.chosen, owner, request_id)
    link = _artifact_link(picture_set, owner, picture_set.language)
    if link is None or picture_set.artifact_version == before:
        return current
    return current_artifact_message(
        [{"role": "assistant", "content": link[0]}, {"role": "user", "content": ""}],
        owner,
    )
