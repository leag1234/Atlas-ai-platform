"""Media embedded in a stored app file: references, embedding and the reverse.

The preview blocks every network access (artifact_routes.PREVIEW_CSP,
connect-src 'none', media-src data: blob:), so a file the person names
(drum samples, pictures) works in an app only when it is inside the file.
Atlas downloads it (services/orchestrator/app_media.py) and the model writes
a short reference instead of the bytes: ``atlas-media:<name>``. This module

- replaces each reference by a stand-in data: URI for the static checks, so
  validation and its size limit apply to the code only (``stand_in``);
- embeds the bytes as data: URIs, with a small decoder for Web Audio
  (``atlasMedia.buffer(...)``, since fetch() of a data: URI is a connection
  the policy blocks) and a manifest of names, sources and licences
  (``embed``);
- turns a stored file back into references and media for a follow-up
  (``abstract``), so the model never reads or rewrites the bytes, and an app
  with embedded media stays editable.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass

SCHEME = "atlas-media:"
REFERENCE = re.compile(r"atlas-media:([a-z0-9][a-z0-9-]{0,47})")
HELPER_ID = "atlas-media-helper"
MANIFEST_ID = "atlas-media"
HELPER = (
    f'<script id="{HELPER_ID}">window.atlasMedia={{buffer:function(u){{'
    "var s=atob(String(u).slice(String(u).indexOf(',')+1)),"
    "a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);"
    "return a.buffer}};</script>"
)
_HELPER_BLOCK = re.compile(
    r'<script id="' + HELPER_ID + r'">.*?</script>\n?', re.DOTALL
)
_MANIFEST_BLOCK = re.compile(
    r'<script type="application/json" id="' + MANIFEST_ID + r'">(.*?)</script>\n?',
    re.DOTALL,
)
_DATA_URI = re.compile(r"data:([a-z]+/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]+=*)")
# Inline data: URIs at least this long are given to the model as references
# too: the model cannot use the bytes and they would fill its context.
INLINE_ABSTRACT_CHARS = 2048
_HEAD = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HTML = re.compile(r"<html(?:\s[^>]*)?>", re.IGNORECASE)


@dataclass(frozen=True)
class Media:
    """One embedded file: its reference name, bytes and provenance."""

    name: str
    mime: str
    data: bytes
    url: str = ""
    filename: str = ""
    licence: str = ""
    # "person" (stated in the request), "page" (the hosting site), "" unknown.
    licence_source: str = ""

    @property
    def reference(self) -> str:
        return SCHEME + self.name

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"

    def stand_in(self) -> str:
        # Unique per name ("kick-2" and "kick2" differ) and plain base64.
        return f"data:{self.mime};base64,QVRMQVM{self.name.encode().hex()}"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.data_uri().encode()).hexdigest()[:24]

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "mime": self.mime,
            "bytes": len(self.data),
            "url": self.url,
            "filename": self.filename,
            "licence": self.licence,
            "licence_source": self.licence_source,
            "sha": self.fingerprint(),
        }


def slug(text: str, taken: set[str]) -> str:
    """A reference name from a file name, unique among ``taken``."""
    stem = text.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    base = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:40] or "file"
    if not base[0].isalnum():
        base = "file-" + base
    name, index = base, 2
    while name in taken:
        name, index = f"{base}-{index}", index + 1
    taken.add(name)
    return name


def references(document: str) -> list[str]:
    """The reference names a document uses, in order, without repeats."""
    seen: list[str] = []
    for match in REFERENCE.finditer(document):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def stand_in(document: str, media: list[Media]) -> str:
    """The document the static checks read: references as short data: URIs."""
    by_name = {item.name: item for item in media}

    def replace(match: re.Match[str]) -> str:
        item = by_name.get(match.group(1))
        return item.stand_in() if item is not None else match.group(0)

    return REFERENCE.sub(replace, document)


def unknown(document: str, media: list[Media]) -> list[str]:
    names = {item.name for item in media}
    return [name for name in references(document) if name not in names]


def _insert(document: str, block: str) -> str:
    for pattern in (_HEAD, _HTML):
        match = pattern.search(document)
        if match is not None:
            return document[: match.end()] + "\n" + block + document[match.end() :]
    return block + document


def embed(document: str, media: list[Media]) -> str:
    """The stored file: stand-ins (or references) replaced by the bytes.

    Only the media the document uses are embedded and listed; the decoder and
    the manifest go first in <head>.
    """
    used = [
        item
        for item in media
        if item.stand_in() in document or item.reference in document
    ]
    if not used:
        return document
    for item in used:
        uri = item.data_uri()
        document = document.replace(item.stand_in(), uri)
        document = re.sub(
            re.escape(item.reference) + r"(?![a-z0-9-])", lambda _: uri, document
        )
    manifest = json.dumps([item.manifest() for item in used], ensure_ascii=False)
    # A manifest value never closes its script element.
    manifest = manifest.replace("</", "<\\/")
    block = (
        f'<script type="application/json" id="{MANIFEST_ID}">{manifest}</script>\n'
        + HELPER
    )
    return _insert(document, block)


def abstract(document: str) -> tuple[str, list[Media]]:
    """A stored file as the model reads it: references instead of bytes.

    The decoder and the manifest are taken out (``embed`` puts them back);
    each embedded file becomes its reference, with its manifest provenance,
    and any other long inline data: URI becomes a reference too.
    """
    entries: dict[str, dict[str, object]] = {}
    match = _MANIFEST_BLOCK.search(document)
    if match is not None:
        try:
            loaded = json.loads(html.unescape(match.group(1).replace("<\\/", "</")))
        except ValueError:
            loaded = []
        for entry in loaded if isinstance(loaded, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("sha"), str):
                entries[entry["sha"]] = entry
    document = _MANIFEST_BLOCK.sub("", document)
    document = _HELPER_BLOCK.sub("", document)
    media: list[Media] = []
    taken: set[str] = set()
    by_uri: dict[str, Media] = {}

    def replace(found: re.Match[str]) -> str:
        uri = found.group(0)
        if uri in by_uri:
            return by_uri[uri].reference
        sha = hashlib.sha256(uri.encode()).hexdigest()[:24]
        entry = entries.get(sha)
        if entry is None and len(uri) < INLINE_ABSTRACT_CHARS:
            return uri
        try:
            data = base64.b64decode(found.group(2), validate=True)
        except ValueError:
            return uri
        if entry is not None:
            wanted = str(entry.get("name") or "")
            name = (
                wanted
                if REFERENCE.fullmatch(SCHEME + wanted) and wanted not in taken
                else slug(wanted or "file", taken)
            )
            taken.add(name)
            item = Media(
                name,
                found.group(1),
                data,
                str(entry.get("url") or ""),
                str(entry.get("filename") or ""),
                str(entry.get("licence") or ""),
                str(entry.get("licence_source") or ""),
            )
        else:
            item = Media(slug("inline", taken), found.group(1), data)
        media.append(item)
        by_uri[uri] = item
        return item.reference

    return _DATA_URI.sub(replace, document), media
