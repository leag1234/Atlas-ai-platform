"""Bounded, inert SKILL.md packages. Parsing never executes or extracts files."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ZIP_BYTES = 1048576
EXPANDED_BYTES = 2097152
FILE_BYTES = 262144
SKILL_BYTES = 32768
MAX_FILES = 64
BUILTINS = Path(__file__).resolve().parents[1] / "skills"


class SkillError(ValueError):
    def __init__(
        self, detail: str, measured: int = 0, limit: int = 0, unit: str = "bytes"
    ) -> None:
        self.measured, self.limit, self.unit = measured, limit, unit
        super().__init__(detail)


def cap(label: str, measured: int, limit: int) -> None:
    if measured > limit:
        raise SkillError(
            f"{label}: measured {measured}; limit {limit}", measured, limit
        )


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 240
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(c) < 32 or c in "\\:%?#" for c in value)
    ):
        raise SkillError("Resource path must be a safe relative POSIX path")
    return value


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    digest: str
    files: dict[str, bytes]

    def metadata(self) -> dict[str, Any]:
        return dict(
            name=self.name,
            description=self.description,
            digest=self.digest,
            size_bytes=sum(map(len, self.files.values())),
        )


def parse(files: dict[str, bytes]) -> Skill:
    cap("file count", len(files), MAX_FILES)
    cap("expanded bytes", sum(map(len, files.values())), EXPANDED_BYTES)
    for name, data in files.items():
        safe_path(name)
        cap("file bytes", len(data), FILE_BYTES)
    if "SKILL.md" not in files:
        raise SkillError("The skill folder must contain SKILL.md")
    cap("SKILL.md bytes", len(files["SKILL.md"]), SKILL_BYTES)
    try:
        text = files["SKILL.md"].decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeError:
        raise SkillError("SKILL.md must be UTF-8") from None
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if match is None:
        raise SkillError("SKILL.md requires YAML front matter between --- lines")
    try:
        # Aliases are not needed for metadata and can hide recursive values.
        if any(
            isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for t in yaml.scan(match[1])
        ):
            raise SkillError("YAML aliases and anchors are not supported")
        node = yaml.compose(match[1], Loader=yaml.SafeLoader)
        if not isinstance(node, yaml.MappingNode):
            raise SkillError("Front matter must be a mapping")
        if any(not isinstance(key, yaml.ScalarNode) for key, _ in node.value):
            raise SkillError("Front matter field names must be scalars")
        keys = [key.value for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise SkillError("Duplicate front matter field")
        metadata = yaml.safe_load(match[1])
    except (yaml.YAMLError, RecursionError):
        raise SkillError("Invalid YAML front matter") from None
    name, description = metadata.get("name"), metadata.get("description")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or len(name) > 64
    ):
        raise SkillError(
            "name must be a lowercase hyphenated slug of at most 64 characters"
        )
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 1024
    ):
        raise SkillError("description must contain 1 to 1024 characters")
    description = " ".join(description.split())
    body = match[2].strip()
    if not body or "\x00" in text:
        raise SkillError(
            "SKILL.md must contain nonempty Markdown instructions without NUL"
        )
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(json.dumps([path, len(data)], separators=(",", ":")).encode())
        digest.update(data)
    return Skill(name, description, body, digest.hexdigest(), files)


def from_zip(data: bytes) -> Skill:
    cap("ZIP bytes", len(data), ZIP_BYTES)
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            cap("archive entries", len(archive.infolist()), MAX_FILES * 2)
            total = 0
            for entry in archive.infolist():
                name = safe_path(entry.filename.rstrip("/"))
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise SkillError("Links and special files are forbidden")
                if entry.flag_bits & 1 or entry.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    raise SkillError("Encrypted or unsupported ZIP entry")
                if name.casefold() in seen:
                    raise SkillError("Duplicate or case-colliding ZIP path")
                seen.add(name.casefold())
                if entry.is_dir():
                    continue
                cap("file bytes", entry.file_size, FILE_BYTES)
                total += entry.file_size
                cap("expanded bytes", total, EXPANDED_BYTES)
                with archive.open(entry) as stream:
                    content = stream.read(FILE_BYTES + 1)
                cap("file bytes", len(content), FILE_BYTES)
                files[name] = content
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        raise SkillError("Invalid ZIP archive") from None
    if "SKILL.md" not in files and files:
        roots = {p.split("/")[0] for p in files}
        if len(roots) == 1 and all("/" in p for p in files):
            files = {p.split("/", 1)[1]: v for p, v in files.items()}
    return parse(files)


@lru_cache(maxsize=1)
def builtins() -> dict[str, Skill]:
    result = {}
    for folder in sorted(BUILTINS.iterdir()):
        if folder.is_dir():
            skill = parse(
                {
                    p.relative_to(folder).as_posix(): p.read_bytes()
                    for p in folder.rglob("*")
                    if p.is_file()
                }
            )
            result[skill.digest] = skill
    return result
