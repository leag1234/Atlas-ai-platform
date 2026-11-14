"""Connected computers: relay gate, signed announcements, tokens, guard, dialogue.

Credential-free. Keys are generated in temporary directories with the system's
ssh-keygen; nothing reaches a network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from services.orchestrator import machine_connect
from services.orchestrator import work_machine_tools as machines
from services.orchestrator.work_remote_tools import written_snapshot
from services.runner import relay, remote_process
from services.runner.broker import Broker
from services.runner.machines import Enrolment, valid_project_dir
from services.runner.policy import RunnerError, decision

ROOT = Path(__file__).resolve().parents[1]
MACHINE = "4f6c2a0e-1b2d-4c3e-8f9a-0123456789ab"
FOLDER = "/home/ada/code/orbit"


def keypair(folder: str) -> tuple[Path, str]:
    path = Path(folder) / "host"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path, path.with_suffix(".pub").read_text().strip()


def sign(key: Path, text: str) -> str:
    return subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "atlas-connect"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class GuardTests(unittest.TestCase):
    def outcome(self, command: str, mode: str = "auto") -> str:
        return decision(command, mode, [], [], [], [], FOLDER)

    def test_ordinary_project_work_runs_without_asking(self) -> None:
        for command in (
            "make test",
            "python3 -m pytest -q",
            "npm run build",
            "git status",
            "git add -A",
            "git commit -m 'Add parser'",
            "git pull --ff-only",
            "mkdir -p build/out",
            "cp src/a.py build/a.py",
            "ssh buildbox uname -a",
            "ssh -o BatchMode=yes buildbox make -C /srv/app test",
            "gh pr view 12",
            "cat README.md",
            "ls ../orbit/src",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.outcome(command), "auto_mode")

    def test_risky_commands_wait_for_the_user(self) -> None:
        for command in (
            "rm -rf build",
            "sudo apt-get install foo",
            "git push origin main",
            "git -C . push",
            "git reset --hard HEAD~1",
            "git clean -fdx",
            "git branch -D old",
            "git checkout -- .",
            "npm publish",
            "docker push registry.example/app",
            "cp build/app /usr/local/bin/app",
            "mv notes.txt ~/notes.txt",
            "touch ../other-project/x",
            "python3 -c 'print(1)'",
            "node -e 'process.exit(0)'",
            "ssh buildbox rm -rf /srv/app",
            "ssh buildbox",
            "ssh buildbox touch /srv/app/x",
            "scp dist/app.tgz buildbox:/srv",
            "find . -name '*.o' -delete",
            "sed -i s/a/b/ ../elsewhere.txt",
            "gh pr merge 12",
            "curl -X POST https://example.org/hook",
            "launchctl unload x",
            "kill 4242",
            "make test && git push",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.outcome(command), "awaiting_approval")

    def test_guard_only_applies_to_connected_computers_and_rules_still_win(
        self,
    ) -> None:
        self.assertEqual(decision("rm -rf build", "auto", [], [], [], []), "auto_mode")
        self.assertEqual(
            decision(
                "git push origin main",
                "auto",
                [],
                [],
                [],
                ["git push upstream *"],
                FOLDER,
            ),
            "awaiting_approval",
        )
        self.assertEqual(
            decision(
                "git push origin main", "auto", [], [], [], ["git push *"], FOLDER
            ),
            "auto_rule",
        )
        self.assertEqual(
            decision("rm -rf build", "auto", [], ["rm *"], [], [], FOLDER), "denied"
        )
        self.assertEqual(self.outcome("make test", "ask"), "awaiting_approval")


class GateTests(unittest.TestCase):
    def test_gate_lists_one_line_per_computer_and_forgets_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            _, first = keypair(folder)
            other = "4f6c2a0e-1b2d-4c3e-8f9a-0123456789ac"
            with patch.dict(os.environ, {"ATLAS_RELAY_GATE": folder + "/gate/hosts"}):
                relay.admit(MACHINE, first)
                relay.admit(other, first + " comment")
                relay.admit(MACHINE, first)
                lines = Path(folder, "gate/hosts").read_text().splitlines()
                self.assertEqual(len(lines), 2)
                self.assertTrue(all(line.startswith("ssh-ed25519 ") for line in lines))
                self.assertTrue(relay.revoke(MACHINE))
                self.assertFalse(relay.revoke(MACHINE))
                self.assertEqual(
                    Path(folder, "gate/hosts").read_text().splitlines(),
                    [" ".join(first.split()[:2]) + " atlas-" + other],
                )
                for bad in ("ssh-rsa AAAA", "ssh-ed25519 AAAA\nssh-ed25519 B", ""):
                    with self.assertRaises(RunnerError):
                        relay.admit(MACHINE, bad)
                with self.assertRaises(RunnerError):
                    relay.admit("../x", first)


class AnnouncementTests(unittest.TestCase):
    def test_only_the_enrolled_key_announces_a_fresh_session(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            key, public = keypair(folder)
            now = int(time.time())
            text = relay.message(MACHINE, "dfQO2yU7r1nqhbS4LCva:Wzo6XToyMjIy", now)
            found = relay.verify(public, text, sign(key, text))
            self.assertEqual(
                found,
                dict(
                    target_id=MACHINE, user="dfQO2yU7r1nqhbS4LCva:Wzo6XToyMjIy", at=now
                ),
            )
            ended = relay.message(MACHINE, None, now + 1)
            self.assertIsNone(relay.verify(public, ended, sign(key, ended))["user"])
            with self.assertRaises(RunnerError):
                relay.verify(public, text.replace("dfQO", "dfQP"), sign(key, text))
            old = relay.message(MACHINE, "dfQO2yU7r1nqhbS4LCva", now - 3600)
            with self.assertRaises(RunnerError):
                relay.verify(public, old, sign(key, old))
            hostile = relay.message(MACHINE, "-oProxyCommand=id", now)
            with self.assertRaises(RunnerError):
                relay.verify(public, hostile, sign(key, hostile))
        with tempfile.TemporaryDirectory() as other:
            other_key, _ = keypair(other)
            with self.assertRaises(RunnerError):
                relay.verify(public, text, sign(other_key, text))


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_PROJECT_IDENTITY_SECRET": "s" * 40,
                "ATLAS_RELAY_CONNECT_ORIGIN": "https://atlas.example.org",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_token_binds_member_machine_and_expiry(self) -> None:
        org = "0d7d8e0e-0000-4000-8000-000000000001"
        token = machine_connect.enrolment_token(
            "ada@example.org", org, MACHINE, "n" * 43
        )
        claims = machine_connect.unseal(token, "enrol")
        self.assertEqual(
            (claims["s"], claims["o"], claims["m"]), ("ada@example.org", org, MACHINE)
        )
        with self.assertRaises(ValueError):
            machine_connect.unseal(token, "machine")
        body, mac = token.split(".")
        forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        forged["m"] = "4f6c2a0e-1b2d-4c3e-8f9a-0123456789ac"
        tampered = (
            base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        )
        with self.assertRaises(ValueError):
            machine_connect.unseal(tampered + "." + mac, "enrol")
        with patch("time.time", return_value=time.time() + 901):
            with self.assertRaises(ValueError):
                machine_connect.unseal(token, "enrol")
        command = machine_connect.connect_command(token)
        self.assertTrue(
            command.startswith(
                "mkdir -p ~/.atlas && curl -fsSL https://atlas.example.org/api/atlas/connect.sh"
            )
        )
        self.assertTrue(
            command.endswith(
                "sh ~/.atlas/connect.sh https://atlas.example.org " + token
            )
        )
        self.assertTrue(machines.install_shown("Run:\n```bash\n" + command + "\n```"))
        # A miscopied token is replaced in place by the exact command.
        retyped = command[:-12] + "XXXXXXXXXXXX"
        answer = "Paste this:\n\n```bash\n" + retyped + "\n```\n\nThen tell me."
        fixed = machines.complete_answer(answer, {"laptop": command}, "en")
        self.assertEqual(fixed, answer.replace(retyped, command))
        self.assertEqual(
            machines.complete_answer("", {"laptop": command}, "en").count(command), 1
        )

    def test_relay_known_host_line_follows_the_dialed_address(self) -> None:
        key = "ssh-ed25519 " + "A" * 68
        self.assertEqual(
            machine_connect.known_host("wss://atlas.example.org/relay/", key),
            "[atlas.example.org]:443 " + key,
        )
        self.assertEqual(
            machine_connect.known_host("ws://edge:8081/relay/", key),
            "[edge]:8081 " + key,
        )


class DialogueTests(unittest.TestCase):
    def test_question_is_written_and_read_back_by_code(self) -> None:
        for lang in ("en", "fr", "de", "es", "it", "pt"):
            block = machines.approval_block(
                "ada-mac", "git push origin main", ".", lang
            )
            answer = "I need to publish the branch.\n\n" + block
            self.assertEqual(
                machines.approval_shown(answer),
                dict(machine="ada-mac", command="git push origin main", cwd="."),
            )
        block = machines.approval_block("ada-mac", "rm -rf build", "web", "fr")
        shown = machines.approval_shown(block)
        assert shown is not None
        self.assertEqual(shown["cwd"], "web")
        self.assertIsNone(machines.approval_shown("```bash\nrm -rf build\n```"))
        two = block + "\n\n" + machines.approval_block("ada-mac", "rm x", ".", "en")
        self.assertIsNone(machines.approval_shown(two))
        self.assertEqual(
            machines.complete_approvals("Done.", {"k": block}), "Done.\n\n" + block
        )
        self.assertEqual(machines.complete_approvals(block, {"k": block}), block)

    def test_only_a_plain_yes_or_no_answers(self) -> None:
        for text in (
            "yes",
            "Oui",
            "oui !",
            "ok",
            "OK vas-y",
            "d'accord",
            "go ahead",
            "Sí",
            "ja",
            "yes please",
            "oui merci",
        ):
            with self.subTest(text=text):
                self.assertTrue(machines.consent(text))
        for text in ("no", "non", "non merci", "don't", "stop", "nein"):
            with self.subTest(text=text):
                self.assertIs(machines.consent(text), False)
        for text in (
            "yes but first run the tests and then push to the staging branch please",
            "what does this command do?",
            "",
            "pourquoi ?",
        ):
            with self.subTest(text=text):
                self.assertIsNone(machines.consent(text))


class FileTests(unittest.TestCase):
    def test_a_written_file_stays_in_the_project(self) -> None:
        command, snapshot = written_snapshot("./src/app.py", "print('hi')\n")
        self.assertEqual(command, "atlas-write src/app.py")
        digest = hashlib.sha256(snapshot).hexdigest()
        auth = dict(
            target=dict(transport="relay"),
            command=dict(command=command, code_digest=digest),
        )
        written = Broker.written(auth, snapshot)
        assert written is not None
        self.assertEqual(written["path"], "src/app.py")
        with self.assertRaises(RunnerError):
            Broker.written(dict(auth, target=dict()), snapshot)
        with self.assertRaises(RunnerError):
            Broker.written(
                dict(
                    auth,
                    command=dict(command="atlas-write other.py", code_digest=digest),
                ),
                snapshot,
            )
        for bad in ("/etc/passwd", "../x", "a/../../x", ".git/config", "."):
            with self.subTest(path=bad), self.assertRaises(Exception):
                written_snapshot(bad, "x")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            encoded = base64.b64encode(b"x = 1\n").decode()
            result = remote_process.write(root, dict(path="src/m.py", content=encoded))
            self.assertEqual((root / "src/m.py").read_text(), "x = 1\n")
            self.assertTrue(result["created"])
            (Path(folder) / "outside").mkdir()
            (root / "link").symlink_to(Path(folder) / "outside")
            for bad in ("link/x.py", "../outside/x.py", ".git/hooks/pre-commit"):
                with self.subTest(path=bad), self.assertRaises(ValueError):
                    remote_process.write(root, dict(path=bad, content=encoded))
            self.assertEqual(list((Path(folder) / "outside").iterdir()), [])


class EnrolmentTests(unittest.TestCase):
    def test_enrolment_input_is_strict(self) -> None:
        valid = dict(
            nonce="n" * 43,
            host_key="ssh-ed25519 " + "A" * 68,
            user="ada",
            os="macos",
            arch="arm64",
            work_root="/Users/ada/My Projects/orbit",
        )
        self.assertEqual(Enrolment.model_validate(valid).work_root, valid["work_root"])
        for change in (
            dict(work_root="/"),
            dict(work_root="relative/path"),
            dict(work_root="/Users/ada/../x"),
            dict(work_root="/Users/ada/x\n"),
            dict(user="-oProxy"),
            dict(os="windows"),
            dict(nonce="short"),
            dict(arch="arm 64"),
            dict(extra=1),
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Enrolment.model_validate(dict(valid, **change))
        self.assertEqual(valid_project_dir("/srv/app/"), "/srv/app")


class ScriptTests(unittest.TestCase):
    def test_connect_script_parses_and_pins_every_download(self) -> None:
        script = ROOT / "services/runner/connect.sh"
        subprocess.run(["sh", "-n", str(script)], check=True)
        text = script.read_text()
        self.assertIn("VERSION=" + machine_connect.UPTERM_VERSION, text)
        for platform in ("darwin_amd64", "darwin_arm64", "linux_amd64", "linux_arm64"):
            self.assertRegex(text, "SUM_" + platform + "=[0-9a-f]{64}\n")
        self.assertIn("--no-forwarding", text)
        self.assertNotIn("--allow-forwarding", text)
        self.assertNotIn("sudo", text.replace("use sudo", ""))

    def test_persistent_mode_writes_a_user_service_only(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            atlas = Path(home, ".atlas")
            atlas.mkdir()
            Path(atlas, "machine.json").write_text(json.dumps(dict(folder="/srv/app")))
            fake = Path(home, "bin")
            fake.mkdir()
            upterm = atlas / "bin"
            upterm.mkdir()
            (upterm / ("upterm-" + machine_connect.UPTERM_VERSION)).write_text(
                "#!/bin/sh\n"
            )
            (upterm / ("upterm-" + machine_connect.UPTERM_VERSION)).chmod(0o755)
            reply = subprocess.run(
                ["sh", str(ROOT / "services/runner/connect.sh"), "--persistent"],
                env=dict(os.environ, HOME=home, ATLAS_CONNECT_DRY_RUN="1"),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(reply.returncode, 0, reply.stderr)
            unit = Path(home, ".config/systemd/user/atlas-connect.service")
            self.assertIn("connect.sh --service", unit.read_text())
            self.assertIn("--stop", reply.stdout)


if __name__ == "__main__":
    unittest.main()
