"""A link to an app version that Atlas did not store never reaches the person.

Only Atlas writes the footer of an app turn (artifact_tools.footer), after it
stored the version. Staging 2026-11-20: a follow-up that brought sample URLs
for the conversation's app was routed to a plain answer, and the model, which
had read four footers in the conversation, wrote one itself for a "version 5"
that was never stored. The side panel opened an empty page (that version
answers 404), and the next app turn, reading that link as the current app,
found it missing and rewrote the app from scratch, losing features.

So in every answer a link to an artifact version is kept only when that version
is stored for this person. Any other one is removed; the first one of an answer
is replaced by the sentence saying the change was not applied and which version
is unchanged (or by nothing when the artifact itself is unknown). The rewriter
streams: text goes out as it comes, except a Markdown link opening ("[") held
until it is known not to be an artifact link, at most LABEL_CHARACTERS plus
TARGET_CHARACTERS characters.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError
from services.orchestrator.artifact_tools import unchanged_footer
from services.orchestrator.stream_client import Sink

LOGGER = logging.getLogger(__name__)

PREFIX = "/api/atlas/artifacts/"
LABEL_CHARACTERS = 300
TARGET_CHARACTERS = 700
SEPARATOR = " · "
LINK = re.compile(
    r"\[(?:[^\[\]\n\\]|\\.){1,%d}\]\(%s([0-9a-f]{32})/versions/([0-9]{1,4})"
    r"/(?:open|download)[^)\s]*\)" % (LABEL_CHARACTERS, re.escape(PREFIX))
)

Stored = Callable[[str, int], bool]
Latest = Callable[[str], int | None]


def store_lookups(owner: str) -> tuple[Stored, Latest]:
    """Whether a version is stored for this owner, and an artifact's latest version."""

    def stored(artifact_id: str, version: int) -> bool:
        try:
            artifact_store.read(artifact_id, owner, version)
        except ArtifactError:
            return False
        return True

    def latest(artifact_id: str) -> int | None:
        try:
            return artifact_store.read(artifact_id, owner).version
        except ArtifactError:
            return None

    return stored, latest


def _possible(text: str) -> bool:
    """True while text (starting with "[") can still become an artifact link."""
    index = 1
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character in "[\n":
            return False
        if character == "]":
            break
        index += 1
    else:
        return len(text) <= LABEL_CHARACTERS + 2
    if index - 1 > LABEL_CHARACTERS:
        return False
    rest = text[index + 1 :]
    if not rest:
        return True
    if not rest.startswith("("):
        return False
    target = rest[1:]
    if len(target) <= len(PREFIX):
        return PREFIX.startswith(target)
    if not target.startswith(PREFIX) or ")" in target:
        # A complete target that LINK did not match is not an artifact link.
        return False
    return not any(c.isspace() for c in target) and len(target) <= TARGET_CHARACTERS


class Rewriter:
    """One pass over an answer, chunk by chunk (feed, then finish)."""

    def __init__(self, language: str, stored: Stored, latest: Latest) -> None:
        self.language = language
        self.stored = stored
        self.latest = latest
        self.pending = ""
        self.swallow = False
        self.said = False
        self.removed = 0
        self._known: dict[tuple[str, int], bool] = {}

    def feed(self, text: str) -> str:
        self.pending += text
        return self._drain(final=False)

    def finish(self) -> str:
        text = self._drain(final=True)
        if self.removed:
            LOGGER.warning("artifact_links removed=%d", self.removed)
        return text

    def _replacement(self, match: re.Match[str]) -> str:
        key = (match.group(1), int(match.group(2)))
        if key not in self._known:
            self._known[key] = self.stored(*key)
        if self._known[key]:
            return match.group(0)
        self.removed += 1
        self.swallow = True
        if self.said:
            return ""
        self.said = True
        version = self.latest(key[0])
        return unchanged_footer(version, self.language) if version else ""

    def _drain(self, *, final: bool) -> str:
        out: list[str] = []
        while self.pending:
            if self.swallow:
                # The " · " between the open and download links of a footer.
                if self.pending.startswith(SEPARATOR):
                    self.pending = self.pending[len(SEPARATOR) :]
                    self.swallow = False
                    continue
                if SEPARATOR.startswith(self.pending) and not final:
                    break
                self.swallow = False
            index = self.pending.find("[")
            if index < 0:
                out.append(self.pending)
                self.pending = ""
                break
            if index:
                out.append(self.pending[:index])
                self.pending = self.pending[index:]
            match = LINK.match(self.pending)
            if match:
                out.append(self._replacement(match))
                self.pending = self.pending[match.end() :]
                continue
            if not final and _possible(self.pending):
                break
            out.append("[")
            self.pending = self.pending[1:]
        return "".join(out)


class Gate:
    """Streams answer text through a Rewriter; other events keep their order."""

    def __init__(self, sink: Sink, rewriter: Rewriter) -> None:
        self.sink = sink
        self.rewriter = rewriter

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        if (
            len(event) == 1
            and isinstance(delta, dict)
            and len(delta) == 1
            and isinstance(delta.get("content"), str)
        ):
            text = self.rewriter.feed(str(delta["content"]))
            if text:
                await self.sink({"delta": {"content": text}})
            return
        await self.flush()
        await self.sink(event)

    async def flush(self) -> None:
        text = self.rewriter.finish()
        if text:
            await self.sink({"delta": {"content": text}})


def checked(text: str, language: str, owner: str) -> tuple[str, int]:
    """The whole answer with links to versions not stored for owner removed."""
    rewriter = Rewriter(language, *store_lookups(owner))
    result = rewriter.feed(text) + rewriter.finish()
    return result, rewriter.removed
