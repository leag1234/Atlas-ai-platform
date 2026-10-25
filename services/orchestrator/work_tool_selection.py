"""REQ-PROJ-05: deterministic, per-turn Work declarations within gateway capacity.

Three tools are always available: plan, exact approval, and group switching.
Each group adds at most five tools. Documents cover reading/comparing/converting
and publishing; spreadsheets/presentations include command-based inspection;
code covers repository scratch edits/tests; files covers discovery/organization.
Switching only changes discovery on the next model step; it grants no authority.
All execution still passes the scoped broker and worker checks.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from services.orchestrator.code_tools import declarations

Group = Literal["documents", "spreadsheets", "presentations", "code", "files"]
CORE = ("update_plan", "request_approval", "switch_tool_group")
GROUPS: dict[str, tuple[str, ...]] = {
    "documents": (
        "read_file",
        "read_document",
        "compare_documents",
        "convert_document",
        "publish_document",
    ),
    "spreadsheets": (
        "read_file",
        "read_document",
        "convert_document",
        "run_command",
        "publish_document",
    ),
    "presentations": (
        "read_file",
        "read_document",
        "convert_document",
        "run_command",
        "publish_document",
    ),
    "code": ("read_file", "search", "edit_file", "write_file", "run_command"),
    "files": ("list_dir", "read_file", "search", "write_file", "organize_files"),
}
PATTERNS = {
    "presentations": r"\b(pptx|slides?|presentation|presentacion|prasentation|diaporama)\b",
    "spreadsheets": r"\b(xlsx|csv|spreadsheet|workbook|excel|tableur|tabellen|foglio|hoja)\b",
    "documents": r"\b(docx|pdf|document|memo|report|rapport|bericht|informe|relazione)\b",
    "code": r"\b(code|repository|repo|git|bug|function|python|javascript|fonction|codigo)\b",
    "files": r"\b(files?|folder|organize|move|rename|fichiers?|dossier|dateien|archivos?)\b",
}


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    group: Group


def select_group(
    question: str, files: list[dict[str, Any]], repository: bool = False
) -> str:
    import unicodedata

    text = "".join(
        c
        for c in unicodedata.normalize("NFKD", question.lower())
        if not unicodedata.combining(c)
    )
    for group, pattern in PATTERNS.items():
        if re.search(pattern, text):
            return group
    suffixes = {
        str(f.get("relative_path", "")).lower().rsplit(".", 1)[-1] for f in files
    }
    mimes = {str(f.get("mime", "")) for f in files}
    if (
        suffixes & {"xlsx", "csv"}
        or "text/csv" in mimes
        or any("spreadsheet" in m for m in mimes)
    ):
        return "spreadsheets"
    if "pptx" in suffixes or any("presentation" in m for m in mimes):
        return "presentations"
    if suffixes & {
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "go",
        "rs",
        "java",
        "c",
        "cpp",
        "sh",
    }:
        return "code"
    if suffixes & {"docx", "pdf", "md", "txt", "html"} or any(
        m.startswith("text/") for m in mimes
    ):
        return "documents"
    return "code" if repository else "files"


class WorkToolSelection:
    def __init__(self, group: str) -> None:
        if group not in GROUPS:
            raise ValueError("Unknown Work tool group")
        self.group = group
        self.pending: str | None = None
        self.catalog = {
            str(t["function"]["name"]): t
            for t in declarations(scoped=True)
            if isinstance(t["function"], dict)
        }
        self.catalog["switch_tool_group"] = dict(
            type="function",
            function=dict(
                name="switch_tool_group",
                description="Select tools for the next step: documents (read/compare/convert/publish), spreadsheets or presentations (read/convert/commands/publish), code (read/search/edit/write/test repository scratch), files (list/read/search/write/organize). Keeps plan and approval tools. Does not grant permissions.",
                parameters=Switch.model_json_schema(),
            ),
        )

    @property
    def names(self) -> tuple[str, ...]:
        return (*CORE, *GROUPS[self.group])

    def declarations(self) -> list[dict[str, object]]:
        # The model asks for declarations at the next dispatch. Already-issued
        # calls in the current batch retain the group they were offered.
        if self.pending is not None:
            self.group, self.pending = self.pending, None
        return [self.catalog[name] for name in self.names]

    def switch(self, arguments: str) -> dict[str, Any]:
        self.pending = Switch.model_validate_json(arguments).group
        return dict(group=self.pending, tools=list((*CORE, *GROUPS[self.pending])))
