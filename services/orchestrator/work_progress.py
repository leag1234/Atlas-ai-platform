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
        "writing_document": "Rédaction du document… {sections} sections, {words} mots",
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
        "writing_document": "Writing the document… {sections} sections, {words} words",
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
        "writing_document": "Dokument wird geschrieben… {sections} Abschnitte, {words} Wörter",
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
        "writing_document": "Redactando el documento… {sections} secciones, {words} palabras",
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
        "writing_document": "Stesura del documento… {sections} sezioni, {words} parole",
    },
}

# The plan of a turn (work_plan.py): its title with the estimate, the heading
# repeated in the final answer, and the steps of an app turn, which the code
# defines (one call writes the app, the code checks it and stores it).
PLAN_LABELS: dict[str, dict[str, str]] = {
    "fr": {
        "title": "Plan",
        "title_estimate": "Plan ({range})",
        "title_prudent": "Plan (quelques minutes)",
        "title_left": "Plan (encore {range})",
        "about": "environ {n} {unit}",
        "between": "{low} à {high} {unit}",
        "under": "moins de {n} {unit}",
        "done_in": "terminé en {duration}",
        "stopped_after": "arrêté après {duration}",
        "app_write": "Écrire l’application",
        "app_change": "Modifier l’application",
        "app_check": "Vérifier l’application",
        "app_deliver": "Vous livrer l’application",
        "state_completed": "fait",
        "state_in_progress": "en cours",
        "state_pending": "à faire",
        "state_failed": "échoué",
    },
    "en": {
        "title": "Plan",
        "title_estimate": "Plan ({range})",
        "title_prudent": "Plan (a few minutes)",
        "title_left": "Plan ({range} left)",
        "about": "about {n} {unit}",
        "between": "{low} to {high} {unit}",
        "under": "under {n} {unit}",
        "done_in": "done in {duration}",
        "stopped_after": "stopped after {duration}",
        "app_write": "Write the app",
        "app_change": "Change the app",
        "app_check": "Check the app",
        "app_deliver": "Hand you the app",
        "state_completed": "done",
        "state_in_progress": "in progress",
        "state_pending": "to do",
        "state_failed": "failed",
    },
    "de": {
        "title": "Plan",
        "title_estimate": "Plan ({range})",
        "title_prudent": "Plan (einige Minuten)",
        "title_left": "Plan (noch {range})",
        "about": "etwa {n} {unit}",
        "between": "{low} bis {high} {unit}",
        "under": "unter {n} {unit}",
        "done_in": "erledigt in {duration}",
        "stopped_after": "angehalten nach {duration}",
        "app_write": "Die App schreiben",
        "app_change": "Die App ändern",
        "app_check": "Die App prüfen",
        "app_deliver": "Ihnen die App übergeben",
        "state_completed": "erledigt",
        "state_in_progress": "läuft",
        "state_pending": "offen",
        "state_failed": "fehlgeschlagen",
    },
    "es": {
        "title": "Plan",
        "title_estimate": "Plan ({range})",
        "title_prudent": "Plan (unos minutos)",
        "title_left": "Plan (quedan {range})",
        "about": "unos {n} {unit}",
        "between": "de {low} a {high} {unit}",
        "under": "menos de {n} {unit}",
        "done_in": "hecho en {duration}",
        "stopped_after": "detenido tras {duration}",
        "app_write": "Escribir la aplicación",
        "app_change": "Modificar la aplicación",
        "app_check": "Comprobar la aplicación",
        "app_deliver": "Entregarle la aplicación",
        "state_completed": "hecho",
        "state_in_progress": "en curso",
        "state_pending": "pendiente",
        "state_failed": "fallido",
    },
    "it": {
        "title": "Piano",
        "title_estimate": "Piano ({range})",
        "title_prudent": "Piano (qualche minuto)",
        "title_left": "Piano (ancora {range})",
        "about": "circa {n} {unit}",
        "between": "da {low} a {high} {unit}",
        "under": "meno di {n} {unit}",
        "done_in": "completato in {duration}",
        "stopped_after": "interrotto dopo {duration}",
        "app_write": "Scrivere l’app",
        "app_change": "Modificare l’app",
        "app_check": "Verificare l’app",
        "app_deliver": "Consegnarle l’app",
        "state_completed": "fatto",
        "state_in_progress": "in corso",
        "state_pending": "da fare",
        "state_failed": "non riuscito",
    },
}
# Units are the same short forms in the five languages, except German minutes.
UNITS = {"minutes": {"de": "Min."}, "hours": {}}


def plan_label(language: str, key: str, **values: object) -> str:
    table = PLAN_LABELS.get(language, PLAN_LABELS["en"])
    return table[key].format(**values)


def unit(language: str, name: str) -> str:
    return UNITS[name].get(language, "min" if name == "minutes" else "h")


def duration_range(language: str, low_seconds: float, high_seconds: float) -> str:
    """'2 to 4 min', 'about 3 min', 'under 2 min', '1 to 2 h': whole units.

    The lower bound is rounded down and the upper bound up, so the shown range
    holds the computed one.
    """
    import math

    hours = high_seconds >= 90 * 60
    size = 3600.0 if hours else 60.0
    name = unit(language, "hours" if hours else "minutes")
    low = math.floor(low_seconds / size)
    high = max(1, math.ceil(high_seconds / size))
    if low < 1:
        return plan_label(language, "under", n=high, unit=name)
    if low >= high:
        return plan_label(language, "about", n=high, unit=name)
    return plan_label(language, "between", low=low, high=high, unit=name)


def elapsed(language: str, seconds: float) -> str:
    """'45 s', '2 min 10 s', '1 h 5 min'."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} {unit(language, 'minutes')} {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} {unit(language, 'minutes')}"


# Activities the chat stream already labels itself (activity-labels.json and the
# guest table): their own label is kept, so one step never has two wordings.
CHAT_ACTIVITIES = {
    "web_search": "web_search",
    "websearch": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "read_document": "reading_document",
    "generate_image": "generate_image",
    "make_pictures": "generate_image",
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

    def writing(self, sections: object, words: object) -> tuple[str, str] | None:
        """The status while Work writes a document as streamed text (document_stream.py).

        Sizes only, never the text: the counts of headings and words so far.
        """
        if type(sections) is not int or type(words) is not int:
            return None
        if sections < 0 or words < 0:
            return None
        self.preparing = False
        table = LABELS[self.language]
        return "work_step", table["writing_document"].format(
            sections=sections, words=words
        )

    def due(self, now: float) -> bool:
        """Whether a status may be sent now (throttled to one per interval)."""
        if (
            self.last_shown is not None
            and now - self.last_shown < MINIMUM_INTERVAL_SECONDS
        ):
            return False
        self.last_shown = now
        return True
