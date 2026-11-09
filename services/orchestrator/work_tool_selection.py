"""REQ-PROJ-05: deterministic, per-turn Work declarations within gateway capacity.

Three tools are always available: plan, exact approval, and group switching.
Each group adds at most five tools. Documents cover reading/comparing/converting
and publishing; spreadsheets/presentations include command-based inspection;
code covers repository scratch edits/tests; files covers discovery/organization.
Switching only changes discovery on the next model step; it grants no authority.
All execution still passes the scoped broker and worker checks.
"""

import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from services.orchestrator.code_tools import declarations

Group = Literal[
    "documents",
    "spreadsheets",
    "presentations",
    "code",
    "files",
    "research",
    "remote",
]
CORE = ("update_plan", "request_approval", "switch_tool_group")
GROUPS: dict[str, tuple[str, ...]] = {
    "remote": ("list_targets", "remote_run", "remote_status", "remote_fetch"),
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
    "code": (
        "edit_file",
        "run_command",
        "remote_run",
        "remote_status",
        "forge_action",
        "read_file",
        "write_file",
        "search",
    ),
    "files": ("list_dir", "read_file", "search", "write_file", "organize_files"),
    "research": ("web_search", "web_fetch"),
}
PATTERNS = {
    "remote": r"\b(remote|target|runner|build server|serveur|registered machine)\b",
    "presentations": r"\b(pptx|slides?|presentation|presentacion|prasentation|diaporama)\b",
    "spreadsheets": r"\b(xlsx|csv|spreadsheet|workbook|excel|tableur|tabellen|foglio|hoja)\b",
    "documents": r"\b(docx|pdf|markdown|(?<![\w./\\-])md|(?<![\w./\\-])txt|word|documents?|documento|memo|report|rapport|bericht|informe|relazione)\b",
    "code": r"\b(code|repository|repo|git|bug|function|python|javascript|fonction|codigo)\b",
    "files": r"\b(files?|folder|organize|move|rename|fichiers?|dossier|dateien|archivos?)\b",
    "research": r"\b(web|internet|online|search|fetch|browse|recherche|rechercher|chercher|recherche web|buscar|busca|buscar en la web|suche|suchen|im web)\b",
}
# A requested file name such as notes.md selects documents only when nothing else matched and there is no repository.
DOCUMENT_EXTENSION = r"\.(?:md|txt)\b"
SOURCE_SUFFIXES = {
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
}
OFFICE_FORMATS = {"pptx", "pdf", "docx", "xlsx"}


class ToolSelectionError(ValueError):
    def __init__(self, reason: str, **result: Any) -> None:
        super().__init__(
            "No remote targets available: " + reason
            if result.get("error") == "remote_unavailable"
            else reason
        )
        self.result = dict(result, reason=reason)


class Switch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    group: str
    focus: str | None = None


def office_format_requested(question: str) -> bool:
    from services.orchestrator.file_content import requested_formats

    return bool(set(requested_formats(question)) & OFFICE_FORMATS)


def code_context(
    question: str, files: list[dict[str, Any]], repository: bool = False
) -> bool:
    import unicodedata

    if repository:
        return True
    if any(
        str(file.get("relative_path", "")).lower().rsplit(".", 1)[-1] in SOURCE_SUFFIXES
        for file in files
    ):
        return True
    text = "".join(
        character
        for character in unicodedata.normalize("NFKD", question.lower())
        if not unicodedata.combining(character)
    )
    return any(
        token.rsplit(".", 1)[-1] in SOURCE_SUFFIXES
        for token in re.split(r"[^\w.]+", text)
        if "." in token
    )


def select_group(
    question: str, files: list[dict[str, Any]], repository: bool = False
) -> str:
    import unicodedata

    text = "".join(
        c
        for c in unicodedata.normalize("NFKD", question.lower())
        if not unicodedata.combining(c)
    )
    if code_context(text, files, repository) and not office_format_requested(text):
        return "code"
    for group, pattern in PATTERNS.items():
        if re.search(pattern, text):
            return group
    if not repository and re.search(DOCUMENT_EXTENSION, text):
        return "documents"
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
    if suffixes & SOURCE_SUFFIXES:
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
        self.groups = dict(GROUPS)
        self.remote_enabled = group == "remote"
        self.group = group
        self.pending: str | None = None
        # Set by the Work turn: true while the request asks for a file that was not produced yet.
        self.deliverable_pending: Callable[[], bool] | None = None
        self.focus: str | None = None
        self.pending_focus: str | None = None
        self.verified = False
        self.reopen_enabled = False
        self.memory_enabled = False
        self.skills_enabled = False
        self.schedules_enabled = False
        self.scheduled_publication = False
        self.catalog = {
            str(t["function"]["name"]): t
            for t in declarations(scoped=True)
            if isinstance(t["function"], dict)
        }
        from services.orchestrator.tools import declarations as chat_declarations

        self.catalog.update(
            {
                str(t["function"]["name"]): t
                for t in chat_declarations()
                if isinstance(t["function"], dict)
                and t["function"]["name"] in {"web_search", "web_fetch"}
            }
        )
        switch_schema = Switch.model_json_schema()
        switch_schema["properties"]["group"]["enum"] = [
            g for g in self.groups if g != "remote"
        ]
        from packages.tool_results import declaration

        self.catalog["reopen_result"] = declaration()
        from packages.project_memory_tool import declaration as memory_declaration

        self.catalog["project_memory"] = memory_declaration()
        self.catalog["switch_tool_group"] = dict(
            type="function",
            function=dict(
                name="switch_tool_group",
                description="Select tools for the next step: documents (read/compare/convert/publish), spreadsheets or presentations (read/convert/commands/publish), code (read/search/edit/write/test repository scratch), files (list/read/search/write/organize), research (web search/fetch). Keeps plan, approval and reopen_result tools. Supply optional focus with any tool from the selected group to bring it into the four available group slots. Does not grant permissions.",
                parameters=switch_schema,
            ),
        )

        if self.remote_enabled:
            self.enable_remote()

    def enable_remote(self) -> None:
        self.remote_enabled = True
        switch = self.catalog["switch_tool_group"]["function"]
        if not isinstance(switch, dict):
            raise ValueError("Invalid switch declaration")
        switch["parameters"]["properties"]["group"]["enum"] = list(self.groups)
        switch["description"] = switch["description"].replace(
            "files (list/read/search/write/organize).",
            "files (list/read/search/write/organize), remote (list targets/run/status/fetch on a granted machine).",
        )

    def add_connectors(self, selection: Any) -> None:
        self.groups.update(selection.groups)
        self.catalog.update(selection.catalog)
        switch = self.catalog["switch_tool_group"]["function"]
        if not isinstance(switch, dict):
            raise ValueError("Invalid switch declaration")
        switch["parameters"]["properties"]["group"]["enum"] = [
            g for g in self.groups if g != "remote" or self.remote_enabled
        ]
        switch["description"] += " Connector groups: " + str(selection.labels)

    def disable_research(self) -> None:
        self.groups.pop("research", None)
        switch = self.catalog["switch_tool_group"]["function"]
        if not isinstance(switch, dict):
            raise ValueError("Invalid switch declaration")
        switch["parameters"]["properties"]["group"]["enum"] = [
            g for g in self.groups if g != "remote" or self.remote_enabled
        ]
        if self.group == "research":
            self.group = "files"
            self.focus = None

    @property
    def names(self) -> tuple[str, ...]:
        group = self.groups[self.group]
        # Memory consumes another declaration slot. Keep document read/publish
        # reachable there while preserving the catalog order for older sessions.
        if self.memory_enabled and self.group in {
            "documents",
            "spreadsheets",
            "presentations",
        }:
            primary = (
                "read_document",
                "publish_document",
                "read_file" if self.group == "documents" else "run_command",
            )
            group = (*primary, *(name for name in group if name not in primary))
        if self.focus in group:
            group = (self.focus, *(name for name in group if name != self.focus))
        if self.group == "code" and self.verified:
            group = (
                "forge_action",
                *(name for name in group if name != "forge_action"),
            )
        extra: tuple[str, ...] = ()
        if self.reopen_enabled:
            extra += ("reopen_result",)
        if self.memory_enabled:
            extra += ("project_memory",)
        if self.skills_enabled:
            extra += ("load_skill",)
        if self.schedules_enabled:
            extra += ("propose_scheduled_task",)
        if self.scheduled_publication:
            extra += ("publish_scheduled_file",)
        selected_group = group if self.group == "code" else group[: 5 - len(extra)]
        names = (*CORE, *extra, *selected_group)
        if len(names) > 16:
            raise ValueError("Work tool selection exceeds the gateway capacity")
        return names

    def declarations(self) -> list[dict[str, object]]:
        # The model asks for declarations at the next dispatch. Already-issued
        # calls in the current batch retain the group they were offered.
        if self.pending is not None:
            self.group, self.pending = self.pending, None
            self.focus, self.pending_focus = self.pending_focus, None
        return [self.catalog[name] for name in self.names]

    def switch(self, arguments: str) -> dict[str, Any]:
        selected = Switch.model_validate_json(arguments)
        if selected.group not in self.groups:
            raise ValueError("Unknown Work tool group")
        if selected.group == "remote" and not self.remote_enabled:
            raise ToolSelectionError(
                "no granted target for this project",
                error="remote_unavailable",
                next_step="Ask an administrator to grant a target to this project, then start a new turn.",
            )
        if (
            selected.focus is not None
            and selected.focus not in self.groups[selected.group]
        ):
            raise ToolSelectionError(
                "focus must name a tool in the selected group",
                error="invalid_tool_focus",
                group=selected.group,
                allowed_tools=list(self.groups[selected.group]),
                next_step="Use one of allowed_tools as focus, or omit focus.",
            )
        current = self.pending or self.group
        if (
            self.deliverable_pending is not None
            and self.deliverable_pending()
            and "publish_document" in self.groups.get(current, ())
            and "publish_document" not in self.groups[selected.group]
        ):
            raise ToolSelectionError(
                "a file was requested and not produced yet",
                error="deliverable_group_required",
                group=current,
                next_step=f"Stay in the {current} group: read the source there, then call publish_document with the content. Do not switch groups before the file exists.",
            )
        self.pending, self.pending_focus = selected.group, selected.focus
        return dict(group=self.pending, tools=list((*CORE, *self.groups[self.pending])))
