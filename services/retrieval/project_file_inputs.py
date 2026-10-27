"""Closed inputs for the frozen task-C file and memory contract."""

from typing import Annotated, Literal
from uuid import UUID
import re

from pydantic import Field, field_validator, model_validator

from services.retrieval.project_policy import Closed, Name
from packages.project_memory import MemoryKind


class FolderCreate(Closed):
    name: Name
    locality: Literal["server", "synced", "local_only"]
    model_use: Literal["deny", "ask", "allow"]
    parent_id: UUID | None = None
    device_id: UUID | None = None
    device_folder_handle: str | None = Field(default=None, min_length=1)


class FolderPatch(Closed):
    name: Name | None = None
    parent_id: UUID | None = None
    locality: Literal["server", "synced", "local_only"] | None = None
    approval_id: UUID | None = None

    @model_validator(mode="after")
    def changes(self) -> "FolderPatch":
        if not self.model_fields_set & {"name", "parent_id", "locality"}:
            raise ValueError("Empty patch")
        if any(getattr(self, k) is None for k in self.model_fields_set - {"parent_id"}):
            raise ValueError("Null field")
        return self


def relative_path(value: str) -> str:
    if (
        not 1 <= len(value) <= 1024
        or re.search(
            r"^(?!/)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)(?!.*\\)[^\x00]+$", value
        )
        is None
    ):
        raise ValueError("Invalid relative path")
    return value


class FilePatch(Closed):
    path: str | None = None
    folder_id: UUID | None = None
    base_version: UUID

    @field_validator("path")
    @classmethod
    def path_valid(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Null path")
        return relative_path(value)

    @model_validator(mode="after")
    def changes(self) -> "FilePatch":
        if not self.model_fields_set & {"path", "folder_id"}:
            raise ValueError("Empty patch")
        if "folder_id" in self.model_fields_set and self.folder_id is None:
            raise ValueError("Null folder")
        return self


class InstructionsPut(Closed):
    text: str = Field(max_length=4000)
    base_version: UUID
    approval_id: UUID


class PageLocator(Closed):
    kind: Literal["page"]
    page: int = Field(ge=1)


class SlideLocator(Closed):
    kind: Literal["slide"]
    slide: int = Field(ge=1)


class ParagraphLocator(Closed):
    kind: Literal["paragraph"]
    paragraph: int = Field(ge=0)


class TableLocator(Closed):
    kind: Literal["table_cell"]
    table: int = Field(ge=0)
    row: int = Field(ge=1)
    column: int = Field(ge=1)


class SheetLocator(Closed):
    kind: Literal["sheet_range"]
    sheet: Name
    range: str = Field(min_length=1)


class TextLocator(Closed):
    kind: Literal["text"]
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class SourceRef(Closed):
    file_id: UUID
    version_id: UUID
    locator: (
        Annotated[
            PageLocator
            | SlideLocator
            | ParagraphLocator
            | TableLocator
            | SheetLocator
            | TextLocator,
            Field(discriminator="kind"),
        ]
        | None
    )


class MemoryCreate(Closed):
    kind: MemoryKind = "fact"
    pinned: bool = False
    status: Literal["proposed", "active"] = "proposed"
    text: str = Field(min_length=1, max_length=500, pattern=r"\S")
    source_refs: list[SourceRef]


class MemoryPatch(Closed):
    kind: MemoryKind | None = None
    pinned: bool | None = None
    text: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"\S")
    status: Literal["proposed", "active", "deleted", "rejected"] | None = None
    approval_id: UUID | None = None

    @model_validator(mode="after")
    def changes(self) -> "MemoryPatch":
        if not self.model_fields_set & {"text", "status", "kind", "pinned"}:
            raise ValueError("Empty patch")
        if any(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("Null field")
        if self.status == "deleted" and self.approval_id is None:
            raise ValueError("Approval required")
        return self
