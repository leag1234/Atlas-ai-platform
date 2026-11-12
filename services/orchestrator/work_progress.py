"""Chat status of a turn that runs in Work behind the chat.

Work reports each step as a project event (a tool call that starts, ends or
fails) and announces each tool call when it starts (a "work_step" stream event,
never persisted). The chat interface renders only the status line of the
stream, so without this relay a document turn showed "Thinking…" for 70 to 200
seconds, then the answer (staging corpus 2026-11-12). Each event becomes a short
status in the conversation language, derived from the event kind and the tool
name only: never from arguments, outputs, paths, commands or user text.

Before the first step, the turn is being set up: scratch project, conversation
and session, workspace, and for code the engine container. That took 17 to 31
seconds of "Thinking…" (staging corpus 2026-11-12); the chat now says the
workspace is being prepared until Work reports its first step.
"""

from collections.abc import Callable, Mapping

# Service-owned UI labels, one table per supported conversation language.
LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "preparing_workspace": "Préparation de l’espace de travail…",
        "planning": "Planification des étapes…",
        "reading_files": "Lecture des fichiers…",
        "writing_file": "Écriture du fichier…",
        "running_command": "Exécution d’une commande…",
        "publishing": "Publication du fichier…",
        "checking": "Vérification du résultat…",
        "preparing_document": "Préparation du document…",
        "machine": "Travail sur la machine…",
        "working": "Travail en cours…",
    },
    "en": {
        "preparing_workspace": "Preparing the workspace…",
        "planning": "Planning the steps…",
        "reading_files": "Reading the files…",
        "writing_file": "Writing the file…",
        "running_command": "Running a command…",
        "publishing": "Publishing the file…",
        "checking": "Checking the result…",
        "preparing_document": "Preparing the document…",
        "machine": "Working on the machine…",
        "working": "Working…",
    },
    "de": {
        "preparing_workspace": "Arbeitsbereich vorbereiten…",
        "planning": "Schritte planen…",
        "reading_files": "Dateien lesen…",
        "writing_file": "Datei schreiben…",
        "running_command": "Befehl ausführen…",
        "publishing": "Datei veröffentlichen…",
        "checking": "Ergebnis prüfen…",
        "preparing_document": "Dokument vorbereiten…",
        "machine": "Arbeit auf dem Rechner…",
        "working": "In Arbeit…",
    },
    "es": {
        "preparing_workspace": "Preparando el espacio de trabajo…",
        "planning": "Planificando los pasos…",
        "reading_files": "Leyendo los archivos…",
        "writing_file": "Escribiendo el archivo…",
        "running_command": "Ejecutando un comando…",
        "publishing": "Publicando el archivo…",
        "checking": "Comprobando el resultado…",
        "preparing_document": "Preparando el documento…",
        "machine": "Trabajando en la máquina…",
        "working": "Trabajando…",
    },
    "it": {
        "preparing_workspace": "Preparazione dell’area di lavoro…",
        "planning": "Pianificazione dei passaggi…",
        "reading_files": "Lettura dei file…",
        "writing_file": "Scrittura del file…",
        "running_command": "Esecuzione di un comando…",
        "publishing": "Pubblicazione del file…",
        "checking": "Verifica del risultato…",
        "preparing_document": "Preparazione del documento…",
        "machine": "Lavoro sulla macchina…",
        "working": "Lavoro in corso…",
    },
}

# Activities the chat stream already labels itself (activity-labels.json and the
# guest table): their own label is kept, so one step never has two wordings.
CHAT_ACTIVITIES = {
    "web_search": "web_search",
    "websearch": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "read_document": "reading_document",
    "generate_image": "generate_image",
}

# Tool names of the Work loop and of the code engine, by step kind.
STEPS = {
    "update_plan": "planning",
    "todowrite": "planning",
    "todoread": "planning",
    "read_file": "reading_files",
    "list_dir": "reading_files",
    "search": "reading_files",
    "read": "reading_files",
    "list": "reading_files",
    "glob": "reading_files",
    "grep": "reading_files",
    "compare_documents": "reading_files",
    "reopen_result": "reading_files",
    "write_file": "writing_file",
    "edit_file": "writing_file",
    "write": "writing_file",
    "edit": "writing_file",
    "multiedit": "writing_file",
    "patch": "writing_file",
    "apply_patch": "writing_file",
    "organize_files": "writing_file",
    "convert_document": "writing_file",
    "run_command": "running_command",
    "bash": "running_command",
    "publish_document": "publishing",
    "publish_scheduled_file": "publishing",
    "list_targets": "machine",
    "remote_run": "machine",
    "remote_fetch": "machine",
    "remote_status": "machine",
}

# Internal bookkeeping steps: the status shown before them stays.
SILENT = frozenset(
    {
        "switch_tool_group",
        "switch_connector_group",
        "load_skill",
        "project_memory",
        "request_approval",
        "propose_scheduled_task",
    }
)

PUBLISHING = frozenset({"publish_document", "publish_scheduled_file"})

# The status changes at most this often; the stream heartbeat shows the latest one.
MINIMUM_INTERVAL_SECONDS = 1.0


def label(language: str, step: str) -> str:
    table = LABELS.get(language, LABELS["en"])
    return table.get(step, table["working"])


def step_kind(tool: str) -> str:
    if tool in STEPS:
        return STEPS[tool]
    if tool.startswith("machine_"):
        return "machine"
    return "working"


class WorkProgress:
    """Turns Work's project events into (activity, description) for the chat status.

    The activity is a key the chat stream already labels (web_search, thinking,
    ...) with an empty description, or "work_step" with a label of this module.
    """

    def __init__(self, language: str, file_turn: Callable[[], bool]) -> None:
        self.language = language if language in LABELS else "en"
        self.file_turn = file_turn
        self.published = False
        self.last_shown: float | None = None
        # Between the hand-over to Work and its first step.
        self.preparing = False

    def phase(self, name: str) -> tuple[str, str] | None:
        """The status for a hand-over phase announced by the chat (chat_work.py).

        "preparing": the turn goes to Work, which sets up its workspace.
        "declined": the chat answers this turn by itself after all.
        """
        if name == "preparing":
            self.preparing = True
            return "work_step", label(self.language, "preparing_workspace")
        if name == "declined":
            self.preparing = False
            return "thinking", ""
        return None

    def _waiting(self) -> tuple[str, str]:
        # Between two steps the model works on the next one: in a file turn,
        # that is the document until it is published, then the final check.
        if self.published:
            return "work_step", label(self.language, "checking")
        if self.file_turn():
            return "work_step", label(self.language, "preparing_document")
        return "thinking", ""

    def _running(self, tool: str) -> tuple[str, str]:
        if tool in CHAT_ACTIVITIES:
            return CHAT_ACTIVITIES[tool], ""
        return "work_step", label(self.language, step_kind(tool))

    def step(self, event: Mapping[str, object]) -> tuple[str, str] | None:
        """The status for one project event, or None to keep the current one."""
        kind = str(event.get("type") or "")
        tool = str(event.get("tool") or "")
        status = str(event.get("status") or "")
        if kind == "workspace":
            # The workspace is ready, but a code turn still starts its engine and
            # the model has not chosen a step yet: the preparing status stays,
            # unless the document is what is being prepared.
            if self.preparing and not self.file_turn():
                return None
            self.preparing = False
            return self._waiting()
        if kind == "plan":
            return None
        if kind != "tool" or not tool or tool in SILENT:
            return None
        self.preparing = False
        if status in {"pending", "running"}:
            return self._running(tool)
        if status == "completed" and tool in PUBLISHING:
            self.published = True
        if status not in {"completed", "failed"}:
            return None
        return self._waiting()

    def due(self, now: float) -> bool:
        """Whether a status may be sent now (throttled to one per interval)."""
        if (
            self.last_shown is not None
            and now - self.last_shown < MINIMUM_INTERVAL_SECONDS
        ):
            return False
        self.last_shown = now
        return True
