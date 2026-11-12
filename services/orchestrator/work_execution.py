"""A Work turn asked to run code runs it, or says it did not.

A request to run code reached Work and the agent answered with the output from
memory, without one tool call, as if it had been run (staging corpus
2026-11-12). Like a requested file, a requested run holds the final answer: the
first answer without any execution is sent back once with the instruction to
run the code and report the real output. If the agent still answers without
running anything, the answer says plainly that nothing was executed.
"""

from collections.abc import Iterable

# Tools that execute code or a command: the loop's sandbox command, a command
# on the user's own machine, and the code engine's shell.
EXECUTION_TOOLS = frozenset({"run_command", "remote_run", "bash"})

NOT_EXECUTED = {
    "en": "Not executed: no code or command was run in this turn, so nothing in this answer is the output of a run.",
    "fr": "Non exécuté : aucun code ni aucune commande n'a été lancé pendant ce tour, donc rien dans cette réponse n'est le résultat d'une exécution.",
    "de": "Nicht ausgeführt: In diesem Durchgang wurde weder Code noch ein Befehl ausgeführt, nichts in dieser Antwort ist also die Ausgabe einer Ausführung.",
    "es": "No ejecutado: en este turno no se ejecutó ningún código ni comando, así que nada en esta respuesta es la salida de una ejecución.",
    "it": "Non eseguito: in questo turno non è stato eseguito alcun codice né comando, quindi nulla in questa risposta è l'output di un'esecuzione.",
}


def run_requested(kind: str | None, text: str) -> bool:
    """The turn asks for code or a command to be executed.

    kind is the routed decision behind the chat when there is one; a project
    turn without it uses the lexical intent, as for a requested file.
    """
    if kind is not None:
        return kind == "run"
    from packages.work_intent import work_reason

    return work_reason(text) == "run"


def run_instruction(tool: str) -> str:
    return (
        "The user asked for code or a command to be run, and nothing has been "
        f"executed in this turn. Run it now with {tool}, then answer, in the language "
        "of the user's message, with the real output of that run. Never present an output that was not produced by a "
        "run. If it cannot run here, say so plainly instead of guessing the output."
    )


class ExecutionGuard:
    """Holds the final answer of a run request until something was executed."""

    def __init__(self, required: bool) -> None:
        self.required = required
        self.executed = False
        self.reminded = False

    def record(self, tools: Iterable[str]) -> None:
        if any(name in EXECUTION_TOOLS for name in tools):
            self.executed = True

    def next_step(self, tool: str, executed: Iterable[str] = ()) -> str | None:
        """The instruction to send back with a final answer, once, or None."""
        self.record(executed)
        if not self.required or self.executed or self.reminded:
            return None
        self.reminded = True
        return run_instruction(tool)

    @property
    def unexecuted(self) -> bool:
        return self.required and not self.executed

    def answer(self, text: str, lang: str) -> tuple[str, bool]:
        """The final answer, with the not-executed notice when nothing ran."""
        if not self.unexecuted:
            return text, False
        notice = NOT_EXECUTED.get(lang, NOT_EXECUTED["en"])
        if notice in text:
            return text, False
        return "\n\n".join(p for p in (notice, text.strip()) if p), True
