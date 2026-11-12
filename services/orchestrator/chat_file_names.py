"""Names of the files a chat turn saves in the hidden chat project.

The hidden "Chats" project is shared by all of a person's chat conversations,
so a name asked for in one of them may already be taken by another. There a
file is never overwritten and a publication never fails on the name: a name
already taken with other content is saved as a numbered copy
("tracker.xlsx", then "tracker (2).xlsx", "tracker (3).xlsx"). Two chat turns
publishing at the same time move the folder revision under each other; the
publication is retried with the current revision (staging 2026-11-12, a
spreadsheet and a cover letter failed with revision_conflict).

Project mode keeps its own rules: this module serves the hidden chat project
only.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

# Publications tried for one file: a taken name or a moved folder each cost one.
MAX_ATTEMPTS = 8
# Numbered copies looked at for one name.
MAX_COPIES = 500


def copy_name(relative: str, number: int) -> str:
    """The numbered copy of a name: copy_name("a/tracker.xlsx", 2) is "a/tracker (2).xlsx"."""
    if number < 2:
        return relative
    path = PurePosixPath(relative)
    return str(path.with_name(f"{path.stem} ({number}){path.suffix}"))


def family(relative: str, taken: Iterable[str]) -> list[str]:
    """The name and its numbered copies that exist, in number order."""
    names = set(taken)
    return [
        name
        for name in (copy_name(relative, n) for n in range(1, MAX_COPIES + 1))
        if name in names
    ]


def free_name(relative: str, taken: Iterable[str]) -> str:
    """The name itself when free, else its first free numbered copy."""
    names = set(taken)
    for number in range(1, MAX_COPIES + 1):
        name = copy_name(relative, number)
        if name not in names:
            return name
    raise ValueError("No free name left for " + PurePosixPath(relative).name)


def last_delivered(
    names: Iterable[str], answers: Iterable[str], project: str
) -> str | None:
    """Of these names, the one this conversation's answers linked last.

    The answers link each saved file under its name ("[Download tracker.xlsx](...)"
    or "[tracker.xlsx](...)", to a route of this project): such a file belongs to
    this conversation, and a change asked here updates it.
    """
    text = "\n".join(answers)
    best: tuple[int, str] | None = None
    for name in names:
        pattern = re.compile(
            r"(?:^|[\[\s])"
            + re.escape(name)
            + r"\]\([^)\s]*/projects/"
            + re.escape(project)
            + r"/"
        )
        for match in pattern.finditer(text):
            if best is None or match.start() > best[0]:
                best = (match.start(), name)
    return best[1] if best else None


def folder_names(existing: Mapping[str, Any], folder_id: str) -> set[str]:
    """Relative paths of the files in one folder, from work_save.project_file_paths."""
    prefix = folder_id + "/"
    return {key[len(prefix) :] for key in existing if key.startswith(prefix)}


async def folder_revision(execution: Any, folder_id: str) -> int:
    """The folder's current revision: each publication there moves it."""
    from services.retrieval.project_identity import ProjectError

    status, listing = await execution.broker.public("GET", "folders")
    if status != 200:
        raise ProjectError(status, str(listing.get("error", "dependency_unavailable")))
    for folder in listing.get("items", []):
        if str(folder["id"]) == folder_id:
            return int(folder["revision"])
    raise ProjectError(404, "not_found")


async def publish_copy(
    publish: Callable[[str, int], Awaitable[Any]],
    relative: str,
    taken: Iterable[str],
    revision: Callable[[], Awaitable[int]],
) -> tuple[dict[str, Any], str]:
    """Publish under the first free name; return the receipt and the name saved.

    A name taken meanwhile moves to the next copy; a folder moved meanwhile by
    another publication is retried with its current revision.
    """
    from services.retrieval.project_identity import ProjectError

    names = set(taken)
    name = free_name(relative, names)
    for attempt in range(MAX_ATTEMPTS):
        try:
            return dict(await publish(name, await revision())), name
        except ProjectError as error:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            if "path_conflict" in error.args:
                names.add(name)
                name = free_name(relative, names)
            elif "revision_conflict" not in error.args:
                raise
    raise AssertionError("unreachable")
