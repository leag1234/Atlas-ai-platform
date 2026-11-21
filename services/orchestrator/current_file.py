"""The current version of the file a chat turn changes: given to the writer, checked after.

PR 588 made a change of a delivered chat file its next version, but the model
still wrote the whole document again from the conversation, so what the person
did not ask to change could drift between versions (a wording, a figure, an
earlier change undone). An app follow-up never had that defect: the code reads
the stored app and gives it to the model as the current version to change
(artifact_tools.current_artifact_message, app_turn.CURRENT_SOURCE). This does
the same for documents, with the same rules:

- the file is the one this conversation's answers linked last
  (chat_file_names.delivered_file, also used by publish_revision), read from
  its stored bytes (packages/doc_render/readback.py), never from the
  conversation text;
- it reaches the writer as trusted context of the Work turn (code_workspace),
  in the arguments publish_document takes, with the instruction to change only
  what is asked;
- it is bounded like the stored app (MAX_CURRENT_ARTIFACT_BYTES there): up to
  FULL_CHARACTERS the whole document; beyond, an outline of every part plus,
  in full, the parts the request names and then the others in order while
  they fit;
- after the new version is built, the two versions are compared
  (readback.changes) for the check before it is claimed (deliverable_check.py).

Project mode has the same guarantees (docs/17, section 12, item 20), with no
router to say whether a turn is a change: the file this conversation's
answers linked last reaches the writer with a conditional instruction
(PROJECT_INSTRUCTION), and a publication that is a delivered file's next
version is compared with that file's stored version (revised) before the
person is asked to approve the overwrite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any

from packages.doc_render import readback

# The whole document up to this size of its arguments (about 10 000 tokens:
# what a writer can also write back in one call); beyond, an outline plus parts.
FULL_CHARACTERS = 40_000
OUTLINE_CHARACTERS = 8_000
PARTS_CHARACTERS = 30_000
LOAD_SECONDS = 20.0

INSTRUCTION = (
    "This turn changes {name}, a file delivered earlier in this conversation. "
    "Its current version is below, read from the stored file (earlier changes "
    "included), in the arguments publish_document takes. Start from it: apply "
    "only the change the person asks for now, keep every other title, heading, "
    "sentence, figure, table cell and slide exactly as it is written there, and "
    "publish the complete new version with publish_document under the path "
    "{name}. Never write it again from the conversation."
)
PROJECT_INSTRUCTION = (
    "{name} is the file delivered last in this conversation. Its current "
    "version is below, read from the stored file (earlier changes included), in "
    "the arguments publish_document takes. If this turn changes {name}, start "
    "from it: apply only the change the person asks for now, keep every other "
    "title, heading, sentence, figure, table cell and slide exactly as it is "
    "written there, and publish the complete new version with publish_document "
    "under the path {name} with strategy regenerated or edited_in_place (it "
    "becomes its next version once the person approves the overwrite). Never "
    "write it again from the conversation. A new deliverable is published "
    "under a new name with strategy created."
)
PARTIAL = (
    " The file is too long to show in full: outline lists every part, and parts "
    "gives the parts the request names, then others in order, in full. Keep "
    "every part as it is; a part you cannot carry over must be named in the "
    "answer as left out."
)


@dataclass
class CurrentFile:
    name: str
    suffix: str
    file_id: str
    version_id: str
    document: readback.Readback
    # Project mode: the writer is told to start from it if the turn changes it.
    project: bool = False

    def context(self, request: str) -> dict[str, Any]:
        """What the writer receives: the whole document, or its outline and parts."""
        arguments = readback.editable(self.document)
        whole = json.dumps(arguments, ensure_ascii=False)
        value: dict[str, Any] = {
            "name": self.name,
            "instruction": (
                PROJECT_INSTRUCTION if self.project else INSTRUCTION
            ).format(name=self.name),
        }
        if len(whole) <= FULL_CHARACTERS or self.document.text is not None:
            if len(whole) > FULL_CHARACTERS:
                # A text file: its first lines, then the outline says how many.
                arguments = {"content": arguments["content"][:FULL_CHARACTERS]}
                value["instruction"] += PARTIAL
                value["outline"] = readback.outline(self.document)
            value["current"] = arguments
            return value
        value["instruction"] += PARTIAL
        outline = readback.outline(self.document)
        while len(json.dumps(outline, ensure_ascii=False)) > OUTLINE_CHARACTERS:
            outline = outline[: max(1, len(outline) * 3 // 4)]
            if len(outline) == 1:
                break
        value["outline"] = outline
        value["title"] = self.document.title
        sections = self.document.sections
        named = [index for index, s in enumerate(sections) if names(request, s)]
        chosen: list[int] = []
        size = 0
        for index in [*named, *range(len(sections))]:
            if index in chosen:
                continue
            part = self.part(index)
            cost = len(json.dumps(part, ensure_ascii=False))
            if size + cost > PARTS_CHARACTERS:
                if index in named:
                    continue
                break
            chosen.append(index)
            size += cost
        value["parts"] = [self.part(index) for index in sorted(chosen)]
        return value

    def part(self, index: int) -> dict[str, Any]:
        section = self.document.sections[index]
        if self.suffix == ".pptx":
            return {"part": index + 1, "slide": readback.slide(section)}
        return {
            "part": index + 1,
            "markdown": readback.section_markdown(
                section, self.document.title if index == 0 else ""
            ),
        }

    def changes(self, raw: bytes) -> dict[str, Any]:
        """The new version against this one (readback.changes), named."""
        after = readback.read(raw, self.suffix)
        return dict(previous_file=self.name, **readback.changes(self.document, after))


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


_WORD = re.compile(r"\w+")
_QUOTED = re.compile(r"[\"“”«»„‘’']([^\"“”«»„‘’']{3,200})[\"“”«»„‘’']")
_NUMBER = re.compile(r"\b(\d{1,3})\b")


def names(request: str, section: readback.Section) -> bool:
    """Whether a request names this part: its heading's words, a quoted text
    of it, or its slide, sheet or page number (any language: no keyword)."""
    asked = _plain(request)
    words = set(_WORD.findall(_plain(section.heading)))
    if words and words <= set(_WORD.findall(asked)):
        return True
    text = _plain(" ".join(section.blocks()))
    if any(_plain(quoted) in text for quoted in _QUOTED.findall(request)):
        return True
    return bool(section.unit) and str(section.unit) in _NUMBER.findall(asked)


async def load(
    tools: Any, project: dict[str, Any], answers: list[str] | None = None
) -> CurrentFile | None:
    """The file this turn changes, read from its stored bytes; None when the
    turn is not a change of a chat file or the file cannot be read (the
    turn then goes on as before, without the comparison).

    In project mode (answers: this conversation's earlier answers), the file
    they linked last, given with the conditional project instruction."""
    chat = bool(
        getattr(tools, "revises_delivered", False)
        and getattr(tools, "numbered_copies", False)
        and getattr(tools, "earlier_answers", None)
    )
    if not chat and (getattr(tools, "numbered_copies", False) or not answers):
        return None
    try:
        async with asyncio.timeout(LOAD_SECONDS):
            return await _load(
                tools,
                str(project["deliverables_folder_id"]),
                list(tools.earlier_answers) if chat else list(answers or []),
                project=not chat,
            )
    except Exception as error:  # noqa: BLE001 - the turn goes on without it
        logging.getLogger(__name__).warning(
            "current_file_unread error=%s", type(error).__name__
        )
        return None


async def _load(
    tools: Any, folder_id: str, answers: list[str], *, project: bool = False
) -> CurrentFile | None:
    from services.orchestrator.chat_file_names import delivered_file
    from services.orchestrator.work_save import project_file_paths

    execution = tools.execution
    existing = await project_file_paths(execution, folder_id)
    wanted = getattr(tools, "requested_suffix", None)
    name = delivered_file(
        existing,
        folder_id,
        answers,
        execution.broker.project,
        {wanted} if isinstance(wanted, str) and wanted else readback.SUFFIXES,
    )
    if name is None:
        return None
    current = await _read(execution, name, existing[folder_id + "/" + name])
    current.project = project
    return current


async def revised(
    tools: Any, folder_id: str, name: str, existing: dict[str, dict[str, Any]]
) -> CurrentFile | None:
    """The stored version of the delivered file a project publication is the
    next version of (project_document_tools.revision_target): the one the
    writer received when it is still current, else read now. None when it
    cannot be read: the version is then published without the comparison."""
    entry = existing.get(folder_id + "/" + name)
    if entry is None:
        return None
    current = getattr(tools, "current_file", None)
    if (
        isinstance(current, CurrentFile)
        and current.name == name
        and current.version_id == str(entry.get("version_id"))
    ):
        return current
    try:
        async with asyncio.timeout(LOAD_SECONDS):
            loaded = await _read(tools.execution, name, entry)
    except Exception as error:  # noqa: BLE001 - published without the comparison
        logging.getLogger(__name__).warning(
            "current_file_unread error=%s", type(error).__name__
        )
        return None
    loaded.project = True
    return loaded


async def _read(execution: Any, name: str, entry: dict[str, Any]) -> CurrentFile:
    from services.retrieval.project_identity import ProjectError

    status, file = await execution.broker.public(
        "GET", "files/" + str(entry["file_id"])
    )
    if status != 200:
        raise ProjectError(status, str(file.get("error", "not_found")))
    raw = await execution.broker.content(
        str(file["id"]), str(file["version_id"]), readback.MAX_BYTES
    )
    if (
        len(raw) != file["size_bytes"]
        or hashlib.sha256(raw).hexdigest() != file["sha256"]
    ):
        raise ProjectError(503, "dependency_unavailable")
    suffix = PurePosixPath(name).suffix.lower()
    document = await asyncio.to_thread(readback.read, raw, suffix)
    return CurrentFile(name, suffix, str(file["id"]), str(file["version_id"]), document)


async def compared(tools: Any, suffix: str, raw: bytes) -> dict[str, Any] | None:
    """The changes of a new version of the turn's current file; None when the
    file published is not one (another format, a new file) or cannot be read."""
    current = getattr(tools, "current_file", None)
    if not isinstance(current, CurrentFile) or current.suffix != suffix:
        return None
    try:
        return await asyncio.to_thread(current.changes, raw)
    except Exception as error:  # noqa: BLE001 - the check goes on without it
        logging.getLogger(__name__).warning(
            "current_file_compare_failed error=%s", type(error).__name__
        )
        return None
