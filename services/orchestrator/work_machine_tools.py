"""Work machine group: the member sets up his own SSH machines in the conversation.

Owner decision of 2026-11-11: no form. The person says "I have a server at this
address, use it over SSH"; the assistant declares it, shows the one command
that installs the Atlas key, waits for "done", verifies and continues the task.

Every call acts as the member through the member machines API
(`/api/atlas/orgs/{org}/machines`, services/retrieval/project_machines.py), with
his own identity: another member's machine does not exist here either. The
private key stays in the runner broker; nothing here reads or returns it.
Every outcome the person must hear comes back as one sentence (`message`) in
the conversation's language, never as a raw code.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Json = dict[str, Any]
NAMES = (
    "machine_add",
    "machine_verify",
    "machine_list",
    "machine_change",
    "machine_forget",
)
Mode = Literal["ask", "rules", "auto"]


class Arguments(BaseModel):
    # Lenient types (a port given as "22"), strict keys: an unknown field is refused.
    model_config = ConfigDict(extra="forbid")


class Add(Arguments):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=253)
    user: str = Field(min_length=1, max_length=64)
    port: int = 22
    permission_mode: Mode | None = None


class Verify(Arguments):
    name: str = Field(min_length=1, max_length=120)
    reset_host_key: bool = False


class Listing(Arguments):
    pass


class Change(Arguments):
    name: str = Field(min_length=1, max_length=120)
    new_name: str | None = Field(default=None, min_length=1, max_length=120)
    permission_mode: Mode | None = None


class Forget(Arguments):
    name: str = Field(min_length=1, max_length=120)
    confirmed: bool = False


SCHEMAS: dict[str, type[Arguments]] = {
    "machine_add": Add,
    "machine_verify": Verify,
    "machine_list": Listing,
    "machine_change": Change,
    "machine_forget": Forget,
}

DESCRIPTIONS = {
    "machine_add": "Declare one of the user's own machines reached over SSH (host name or IPv4 address, account, port). Atlas creates a key for it and keeps the private key. Returns install_command: show it verbatim in a ```bash block, ask the user to run it in a terminal on their own computer and to say when it is done, then end your turn. Call once per machine; name it after its use (build, tests) when the user gave no name.",
    "machine_verify": "Connect to a declared machine after the user said the install command was run: pins the host key on first use, checks the account and returns its status. On failure relay message. reset_host_key=true only after the user confirmed the server was reinstalled.",
    "machine_list": "List the user's own machines: name, address, status (verified or not), permission mode. No credentials.",
    "machine_change": "Rename a machine or change its permission mode (auto: commands run without asking; rules or ask: commands wait for the user's approval).",
    "machine_forget": "Remove a machine from Atlas and destroy its Atlas key. Refused unless confirmed=true, which you set only after the user said yes to removing that machine.",
}


def declarations() -> list[dict[str, object]]:
    return [
        dict(
            type="function",
            function=dict(
                name=name,
                description=DESCRIPTIONS[name],
                parameters=schema.model_json_schema(),
            ),
        )
        for name, schema in SCHEMAS.items()
    ]


# One sentence per outcome, in the conversation's language; the model relays it.
# Other languages get the English sentence and the instruction to translate it.
MESSAGES: dict[str, dict[str, str]] = {
    "declared": {
        "en": "The machine {name} ({target}) is declared. Atlas created a key for it. Run this command in a terminal on your own computer (it adds the Atlas key to the account {user}, you will be asked for your usual SSH password or key), then tell me when it is done.",
        "fr": "La machine {name} ({target}) est déclarée. Atlas a créé une clé pour elle. Lancez cette commande dans un terminal sur votre ordinateur (elle ajoute la clé Atlas au compte {user} ; votre mot de passe ou votre clé SSH habituels vous seront demandés), puis dites-moi quand c'est fait.",
    },
    "already_declared": {
        "en": "The machine {name} ({target}) was already declared and is not verified yet. Run this command in a terminal on your own computer, then tell me when it is done.",
        "fr": "La machine {name} ({target}) était déjà déclarée et n'est pas encore vérifiée. Lancez cette commande dans un terminal sur votre ordinateur, puis dites-moi quand c'est fait.",
    },
    "already_verified": {
        "en": "The machine {name} ({target}) is already set up and verified: it can be used directly.",
        "fr": "La machine {name} ({target}) est déjà configurée et vérifiée : elle peut être utilisée directement.",
    },
    "name_taken": {
        "en": "A machine named {name} already exists with another address ({target}). Choose another name, or remove that machine first.",
        "fr": "Une machine nommée {name} existe déjà avec une autre adresse ({target}). Choisissez un autre nom, ou supprimez d'abord cette machine.",
    },
    "mode_auto": {
        "en": "On {name}, the commands Atlas runs will run without asking you each time.",
        "fr": "Sur {name}, les commandes lancées par Atlas s'exécuteront sans vous demander à chaque fois.",
    },
    "mode_auto_chat": {
        "en": "On {name}, the commands Atlas runs will run without asking you each time. If you prefer to approve each command, say so (approvals cannot be shown in this conversation yet, so commands on that machine would then not run here).",
        "fr": "Sur {name}, les commandes lancées par Atlas s'exécuteront sans vous demander à chaque fois. Si vous préférez approuver chaque commande, dites-le (les approbations ne peuvent pas encore s'afficher dans cette conversation : les commandes sur cette machine ne s'exécuteraient alors plus ici).",
    },
    "mode_approval_chat": {
        "en": "On {name}, each command now waits for an approval, which this conversation cannot show yet: commands on that machine will not run here until its mode is auto again.",
        "fr": "Sur {name}, chaque commande attend désormais une approbation, que cette conversation ne peut pas encore afficher : les commandes sur cette machine ne s'exécuteront plus ici tant que son mode ne sera pas de nouveau auto.",
    },
    "mode_default": {
        "en": "On {name}, commands outside the allowed rules wait for your approval.",
        "fr": "Sur {name}, les commandes hors des règles autorisées attendent votre approbation.",
    },
    "invalid_input": {
        "en": "That machine was refused: give a public host name or IPv4 address, a plain account name (letters, digits, - _ .) and a port between 1 and 65535.",
        "fr": "Cette machine a été refusée : indiquez un nom d'hôte public ou une adresse IPv4, un nom de compte simple (lettres, chiffres, - _ .) et un port entre 1 et 65535.",
    },
    "limit": {
        "en": "You already have the maximum of 20 machines: remove one before adding another.",
        "fr": "Vous avez déjà le maximum de 20 machines : supprimez-en une avant d'en ajouter une autre.",
    },
    "unknown": {
        "en": "There is no machine named {name}. {known}",
        "fr": "Il n'y a pas de machine nommée {name}. {known}",
    },
    "known": {
        "en": "Your machines: {names}.",
        "fr": "Vos machines : {names}.",
    },
    "none": {
        "en": "You have no machine yet.",
        "fr": "Vous n'avez encore aucune machine.",
    },
    "awaiting_user": {
        "en": "The machine {name} was declared in this same turn: its key cannot be installed yet. Show the install command and wait for the user to say it is done.",
        "fr": "La machine {name} vient d'être déclarée dans ce même tour : sa clé ne peut pas encore être installée. Montrez la commande d'installation et attendez que l'utilisateur dise que c'est fait.",
    },
    "verified": {
        "en": "The machine {name} is verified: Atlas is connected as {target}. Its host key fingerprint is {fingerprint}.",
        "fr": "La machine {name} est vérifiée : Atlas est connecté en {target}. L'empreinte de sa clé d'hôte est {fingerprint}.",
    },
    "credential_unavailable": {
        "en": "The machine {name} refused the Atlas key: the installation command has not been run on it yet, or not for the account {user}. Run this command in a terminal on your own computer, then tell me when it is done.",
        "fr": "La machine {name} a refusé la clé Atlas : la commande d'installation n'y a pas encore été lancée, ou pas pour le compte {user}. Lancez cette commande dans un terminal sur votre ordinateur, puis dites-moi quand c'est fait.",
    },
    "host_key_fingerprint_mismatch": {
        "en": "The machine {name} answered with a different host key than the one recorded at its first verification. This happens when a server is reinstalled, but it can also mean that someone is intercepting the connection. Was this server reinstalled? Only if you confirm will Atlas record its new key.",
        "fr": "La machine {name} a répondu avec une clé d'hôte différente de celle enregistrée lors de sa première vérification. Cela arrive quand un serveur est réinstallé, mais cela peut aussi signifier que quelqu'un intercepte la connexion. Ce serveur a-t-il été réinstallé ? Atlas n'enregistrera sa nouvelle clé que si vous le confirmez.",
    },
    "runner_target_address_denied": {
        "en": "The address of {name} is on a private or reserved network, which this Atlas instance cannot reach yet: the machine needs a public address.",
        "fr": "L'adresse de {name} est sur un réseau privé ou réservé, que cette instance d'Atlas ne peut pas encore atteindre : la machine doit avoir une adresse publique.",
    },
    "runner_target_unresolved": {
        "en": "The host name of {name} ({host}) does not resolve to an address: check its spelling.",
        "fr": "Le nom d'hôte de {name} ({host}) ne correspond à aucune adresse : vérifiez son orthographe.",
    },
    "runner_transport_failed": {
        "en": "Atlas could not open an SSH connection to {name} ({host}, port {port}): the machine may be off, a firewall may block that port, or SSH may listen on another port.",
        "fr": "Atlas n'a pas pu ouvrir de connexion SSH vers {name} ({host}, port {port}) : la machine est peut-être éteinte, un pare-feu bloque peut-être ce port, ou SSH écoute sur un autre port.",
    },
    "invalid_work_root": {
        "en": "Atlas connected to {name} but could not prepare its working folder in the account's home directory.",
        "fr": "Atlas s'est connecté à {name} mais n'a pas pu préparer son dossier de travail dans le répertoire personnel du compte.",
    },
    "verify_failed": {
        "en": "The verification of {name} did not succeed; try again in a moment.",
        "fr": "La vérification de {name} n'a pas abouti ; réessayez dans un moment.",
    },
    "changed": {
        "en": "The machine {name} is updated.",
        "fr": "La machine {name} est mise à jour.",
    },
    "nothing_to_change": {
        "en": "Nothing to change: give a new name or a permission mode.",
        "fr": "Rien à changer : indiquez un nouveau nom ou un mode d'autorisation.",
    },
    "confirm_forget": {
        "en": "Removing {name} destroys its Atlas key and its working folders that can still be reached. Ask the user to confirm; call again with confirmed=true only after they said yes.",
        "fr": "Supprimer {name} détruit sa clé Atlas et ses dossiers de travail encore joignables. Demandez confirmation à l'utilisateur ; rappelez avec confirmed=true seulement après son accord.",
    },
    "forgotten": {
        "en": 'The machine {name} is removed from Atlas and its Atlas key is destroyed. You can also delete the line ending in "atlas" from ~/.ssh/authorized_keys on the machine.',
        "fr": "La machine {name} est retirée d'Atlas et sa clé Atlas est détruite. Vous pouvez aussi supprimer la ligne qui se termine par « atlas » dans ~/.ssh/authorized_keys sur la machine.",
    },
    "commands_active": {
        "en": "Commands are still running on {name}: they must finish or be cancelled before it can be removed.",
        "fr": "Des commandes tournent encore sur {name} : elles doivent finir ou être annulées avant de pouvoir la supprimer.",
    },
    "conflict": {
        "en": "The machine {name} changed meanwhile; try again.",
        "fr": "La machine {name} a changé entre-temps ; réessayez.",
    },
    "unavailable": {
        "en": "The machine service is unavailable at the moment; try again in a few minutes.",
        "fr": "Le service des machines est indisponible pour le moment ; réessayez dans quelques minutes.",
    },
    "disabled": {
        "en": "Remote machines are not enabled on this Atlas instance.",
        "fr": "Les machines distantes ne sont pas activées sur cette instance d'Atlas.",
    },
}
# Probe errors with the same meaning for the person.
ALIASES = {
    "runner_connection_unknown": "runner_transport_failed",
    "runner_host_key_unavailable": "runner_transport_failed",
    "credential_fingerprint_mismatch": "credential_unavailable",
}
RELAY = "Relay message to the user in their language; never show error codes."


def say(key: str, lang: str, **values: object) -> str:
    words = MESSAGES[key]
    return words.get(lang, words["en"]).format(**values)


def failure(key: str, lang: str, error: str, **values: object) -> Json:
    return dict(error=error, message=say(key, lang, **values), next_step=RELAY)


def public(machine: Json) -> Json:
    """What the model sees of a machine: no credential reference, no key material."""
    fingerprints = machine.get("host_key_fingerprints") or []
    return dict(
        name=machine.get("name"),
        host=machine.get("host"),
        user=machine.get("user"),
        port=machine.get("port"),
        status=machine.get("status"),
        verified=bool(machine.get("verified_at")),
        permission_mode=machine.get("permission_mode"),
        host_key_fingerprint=fingerprints[0] if fingerprints else None,
    )


def target(machine: Json) -> str:
    port = machine.get("port", 22)
    address = f"{machine.get('user')}@{machine.get('host')}"
    return address if port == 22 else f"{address}, port {port}"


def fenced(command: str) -> str:
    return "```bash\n" + command + "\n```"


def lang_of(tools: Any) -> str:
    return str(getattr(tools, "lang", "") or "en")


async def listing(tools: Any) -> list[Json] | Json:
    status, body = await tools.execution.broker.member("GET", "machines")
    if status != 200 or not isinstance(body.get("items"), list):
        return failure("unavailable", lang_of(tools), "dependency_unavailable")
    return [item for item in body["items"] if isinstance(item, dict)]


def find(machines: list[Json], name: str) -> Json | None:
    exact = [m for m in machines if m.get("name") == name]
    if exact:
        return exact[0]
    folded = [
        m
        for m in machines
        if str(m.get("name", "")).casefold().strip() == name.casefold().strip()
    ]
    return folded[0] if len(folded) == 1 else None


def unknown(machines: list[Json], name: str, lang: str) -> Json:
    known = (
        say("known", lang, names=", ".join(str(m.get("name")) for m in machines))
        if machines
        else say("none", lang)
    )
    return failure("unknown", lang, "machine_not_found", name=name, known=known)


async def resolve(tools: Any, name: str) -> Json:
    found = await listing(tools)
    if isinstance(found, dict):
        return found
    machine = find(found, name)
    if machine is None:
        return unknown(found, name, lang_of(tools))
    return dict(machine=machine)


def unattended(tools: Any) -> bool:
    return bool(getattr(tools, "approvals_unattended", False))


def mode_note(tools: Any, machine: Json) -> str:
    """What the permission mode of this machine means for the person, here."""
    name, lang = machine.get("name"), lang_of(tools)
    if machine.get("permission_mode") == "auto":
        return say(
            "mode_auto_chat" if unattended(tools) else "mode_auto", lang, name=name
        )
    # rules or ask in the chat: no approval card can be answered there yet.
    return say(
        "mode_approval_chat" if unattended(tools) else "mode_default", lang, name=name
    )


def awaiting(tools: Any, machine: Json, key: str) -> Json:
    """A declared machine waiting for its key: the command, verbatim, and what to do."""
    command = str(machine["install_command"])
    tools.machine_commands[str(machine["name"])] = command
    lang = lang_of(tools)
    return dict(
        machine=public(machine),
        install_command=command,
        message=say(
            key,
            lang,
            name=machine["name"],
            target=target(machine),
            user=machine["user"],
        ),
        permission_note=mode_note(tools, machine),
        next_step=(
            "Show install_command verbatim in a ```bash block with message and "
            "permission_note in the user's language, then end your turn. Call "
            "machine_verify only after the user says it is done."
        ),
    )


async def add(tools: Any, args: Add) -> Json:
    lang = lang_of(tools)
    body: Json = dict(name=args.name, host=args.host, user=args.user, port=args.port)
    mode = args.permission_mode or ("auto" if unattended(tools) else None)
    if mode is not None:
        body["permission_mode"] = mode
    status, created = await tools.execution.broker.member(
        "POST", "machines", body, timeout=30
    )
    if status == 201 and isinstance(created.get("install_command"), str):
        tools.machines_declared.add(str(created["name"]))
        return awaiting(tools, created, "declared")
    error = str(created.get("error", ""))
    if status == 409 and error == "runner_name_conflict":
        found = await listing(tools)
        machine = None if isinstance(found, dict) else find(found, args.name)
        if machine is None:
            return failure("unavailable", lang, "dependency_unavailable")
        same = (
            machine.get("host") == args.host
            and machine.get("user") == args.user
            and machine.get("port") == args.port
        )
        if not same:
            return failure(
                "name_taken",
                lang,
                "runner_name_conflict",
                name=machine["name"],
                target=target(machine),
            )
        if machine.get("verified_at"):
            return dict(
                machine=public(machine),
                message=say(
                    "already_verified",
                    lang,
                    name=machine["name"],
                    target=target(machine),
                ),
                next_step="Continue the user's request on this machine.",
            )
        return awaiting(tools, machine, "already_declared")
    if status == 409 and error == "runner_machine_limit":
        return failure("limit", lang, error)
    if status == 422:
        return failure("invalid_input", lang, "invalid_machine")
    return failure("unavailable", lang, "dependency_unavailable")


async def verify(tools: Any, args: Verify) -> Json:
    lang = lang_of(tools)
    found = await resolve(tools, args.name)
    if "machine" not in found:
        return found
    machine = found["machine"]
    name = str(machine["name"])
    if name in tools.machines_declared and not machine.get("verified_at"):
        # A key made a moment ago cannot be installed yet: no connection attempt,
        # no loop. The turn ends with the command; the next turn verifies.
        return dict(
            failure("awaiting_user", lang, "awaiting_user", name=name),
            install_command=machine["install_command"],
        )
    status, checked = await tools.execution.broker.member(
        "POST",
        f"machines/{machine['id']}/verify",
        dict(reset_host_key=True) if args.reset_host_key else {},
        timeout=90,
    )
    if status == 404:
        return failure("unknown", lang, "machine_not_found", name=name, known="")
    if status != 200 or not isinstance(checked.get("status"), str):
        return failure("unavailable", lang, "dependency_unavailable")
    if checked["status"] == "available":
        tools.machine_commands.pop(name, None)
        selection = getattr(tools, "selection", None)
        if selection is not None:
            # Verified within this turn: its commands are reachable at once.
            selection.enable_remote()
        fingerprints = checked.get("host_key_fingerprints") or ["?"]
        return dict(
            machine=public(checked),
            message=say(
                "verified",
                lang,
                name=name,
                target=target(checked),
                fingerprint=fingerprints[0],
            ),
            next_step=(
                f'Continue the user\'s original request now: remote_run with target "{name}" '
                "(read-only diagnosis first). Do not ask the user to repeat the request."
            ),
        )
    probe = checked.get("last_probe") or {}
    error = str(probe.get("error", "") or "verify_failed")
    key = ALIASES.get(error, error)
    if key not in {
        "credential_unavailable",
        "host_key_fingerprint_mismatch",
        "runner_target_address_denied",
        "runner_target_unresolved",
        "runner_transport_failed",
        "invalid_work_root",
    }:
        key = "verify_failed"
    result = failure(
        key,
        lang,
        error,
        name=name,
        host=checked.get("host", machine.get("host")),
        port=checked.get("port", machine.get("port")),
        user=checked.get("user", machine.get("user")),
    )
    result["machine"] = public(checked)
    if key == "credential_unavailable":
        command = str(checked.get("install_command") or machine["install_command"])
        tools.machine_commands[name] = command
        result["install_command"] = command
        result["next_step"] = (
            RELAY
            + " Show install_command verbatim in a ```bash block and end your turn."
        )
    if key == "host_key_fingerprint_mismatch":
        result["next_step"] = (
            RELAY + " Ask whether the server was reinstalled; only after the user "
            "confirms, call machine_verify with reset_host_key=true."
        )
    return result


async def change(tools: Any, args: Change) -> Json:
    lang = lang_of(tools)
    if args.new_name is None and args.permission_mode is None:
        return failure("nothing_to_change", lang, "empty_change")
    found = await resolve(tools, args.name)
    if "machine" not in found:
        return found
    machine = found["machine"]
    body: Json = {}
    if args.new_name is not None:
        body["name"] = args.new_name
    if args.permission_mode is not None:
        body["permission_mode"] = args.permission_mode
    status, changed = await tools.execution.broker.member(
        "PATCH", f"machines/{machine['id']}", body, revision=machine.get("revision")
    )
    if status == 200 and isinstance(changed.get("name"), str):
        return dict(
            machine=public(changed),
            message=say("changed", lang, name=changed["name"]),
            permission_note=mode_note(tools, changed),
        )
    error = str(changed.get("error", ""))
    if status == 409 and error == "runner_name_conflict":
        return failure(
            "name_taken", lang, error, name=args.new_name, target=target(machine)
        )
    if status in {409, 412} and "revision" in error:
        return failure("conflict", lang, error, name=machine["name"])
    if status == 422:
        return failure("invalid_input", lang, "invalid_machine")
    return failure("unavailable", lang, "dependency_unavailable")


async def forget(tools: Any, args: Forget) -> Json:
    lang = lang_of(tools)
    found = await resolve(tools, args.name)
    if "machine" not in found:
        return found
    machine = found["machine"]
    name = str(machine["name"])
    if not args.confirmed:
        return failure("confirm_forget", lang, "confirmation_required", name=name)
    status, deleted = await tools.execution.broker.member(
        "DELETE",
        f"machines/{machine['id']}",
        None,
        revision=machine.get("revision"),
        timeout=60,
    )
    if status == 200 and deleted.get("deleted"):
        tools.machine_commands.pop(name, None)
        return dict(name=name, removed=True, message=say("forgotten", lang, name=name))
    error = str(deleted.get("error", ""))
    if status == 409 and error == "runner_commands_active":
        return failure("commands_active", lang, error, name=name)
    if status in {409, 412} and "revision" in error:
        return failure("conflict", lang, error, name=name)
    return failure("unavailable", lang, "dependency_unavailable")


async def execute(tools: Any, name: str, arguments: str) -> Json:
    """Run one machine tool as the member; every refusal is a relayable sentence."""
    from pydantic import ValidationError

    from services.retrieval.project_identity import ProjectError

    lang = lang_of(tools)
    if os.environ.get("ATLAS_REMOTE_RUNNERS") != "1":
        return failure("disabled", lang, "remote_runners_disabled")
    try:
        args = SCHEMAS[name].model_validate_json(arguments or "{}")
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        if name == "machine_add" and fields and set(fields) <= {"host", "user", "port"}:
            # The person's own address or account: refused as a sentence, as
            # the backend refuses it, never echoed back.
            return failure("invalid_input", lang, "invalid_machine")
        return dict(
            error="invalid_tool_arguments",
            fields=fields,
            next_step="Correct these arguments from the tool schema and call again.",
        )
    try:
        if isinstance(args, Add):
            return await add(tools, args)
        if isinstance(args, Verify):
            return await verify(tools, args)
        if isinstance(args, Change):
            return await change(tools, args)
        if isinstance(args, Forget):
            return await forget(tools, args)
        found = await listing(tools)
        if isinstance(found, dict):
            return found
        return dict(
            machines=[public(m) for m in found],
            **({} if found else {"message": say("none", lang)}),
        )
    except ProjectError:
        return failure("unavailable", lang, "dependency_unavailable")


def machine_turn(request: Any, routed: Any, repository: bool) -> bool:
    """A Work turn that starts in the remote group, with the machine tools.

    The router said "remote"; or, without the router, the request names one of
    the person's machines (outside a repository session, where the code group
    already carries the remote commands); or it answers a pending setup.
    """
    if os.environ.get("ATLAS_REMOTE_RUNNERS") != "1":
        return False
    if request.__dict__.get("machine_setup_pending"):
        return True
    if routed is not None:
        return bool(routed.kind == "remote")
    from packages.work_intent import work_reason

    return not repository and work_reason(request.user_text) == "remote"


_INSTALL = re.compile(
    r'echo "ssh-ed25519 [A-Za-z0-9+/]{40,1024}={0,2} atlas" >> ~/\.ssh/authorized_keys'
)


def install_shown(text: str) -> bool:
    """Whether a text carries an Atlas key installation command."""
    return _INSTALL.search(text) is not None


LEAD = {
    "en": "Run this command in a terminal on your own computer, then tell me when it is done:",
    "fr": "Lancez cette commande dans un terminal sur votre ordinateur, puis dites-moi quand c'est fait :",
    "de": "Führen Sie diesen Befehl in einem Terminal auf Ihrem eigenen Computer aus und sagen Sie mir, wenn es erledigt ist:",
    "es": "Ejecuta este comando en un terminal de tu propio ordenador y dime cuando esté hecho:",
    "it": "Esegui questo comando in un terminale sul tuo computer, poi dimmi quando hai fatto:",
}


def complete_answer(answer: str, commands: dict[str, str], lang: str) -> str:
    """Every install command of this turn reaches the person, verbatim, copyable.

    The model is told to show it; this guarantees it: a missing or retyped key
    would make the next verification fail with no way for the person to see why.
    """
    missing = [
        (name, command)
        for name, command in commands.items()
        if fenced(command) not in answer
        and not re.search(r"```[A-Za-z]*\n" + re.escape(command) + r"\n```", answer)
    ]
    if not missing:
        return answer
    lead = LEAD.get(lang, LEAD["en"])
    blocks = "\n\n".join(
        (f"**{name}**\n\n" if len(missing) > 1 else "") + fenced(command)
        for name, command in missing
    )
    return (answer.rstrip() + "\n\n" if answer.strip() else "") + lead + "\n\n" + blocks


INSTRUCTIONS = """

Machines. The user's own computers are reached over SSH with machine_add, machine_verify, machine_list, machine_change and machine_forget (remote group); once verified, list_targets and remote_run use them by name.
- Never ask the user for a password, a private key or their own SSH key: Atlas makes its own key for each machine and keeps it.
- To add machines: call machine_add once per machine. Show each install_command exactly as returned, in its own ```bash block, with message and permission_note, ask the user to run it in a terminal on their own computer and to say when it is done, then end your turn. Never ask them to run anything else on the server.
- When the user says it is done (in any words): call machine_verify for each machine waiting for it, then continue the user's original request of this conversation without asking them to repeat it.
- Relay each tool message in the user's language; never show error codes.
- Diagnose read-only first (status, logs, disk space) before any command that changes the machine.
- Host key changed: explain the risk and ask whether the server was reinstalled; call machine_verify with reset_host_key=true only after the user confirms.
- machine_forget: set confirmed=true only when the user's last message says yes to removing that machine."""

CHAT_MODE = """
- In this conversation new machines use permission mode auto (no approval can be shown here): tell the user that commands will run on that machine without asking each time and that they can refuse; if they refuse, call machine_change with permission_mode rules."""

PROJECT_MODE = """
- New machines use the default permission mode: commands outside the allowed rules wait for the user's approval."""


def instructions(unattended_turn: bool) -> str:
    return INSTRUCTIONS + (CHAT_MODE if unattended_turn else PROJECT_MODE)
