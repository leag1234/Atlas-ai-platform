import argparse
import ast
import pathlib
import re
import shlex
import sys

sys.path.insert(0, "/work")
from taskq.cli import build_parser  # noqa: E402

problems = []
doc_path = pathlib.Path("/work/docs/CLI.md")
if not doc_path.exists():
    print("docs/CLI.md missing")
    sys.exit(1)
doc = doc_path.read_text(encoding="utf-8")
parser = build_parser()
sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
sections = {}
parts = re.split(r"^## `taskq ([a-z]+)`[ \t]*$", doc, flags=re.M)
for i in range(1, len(parts), 2):
    sections[parts[i]] = parts[i + 1]
for name, subparser in sub.choices.items():
    text = sections.get(name)
    if text is None:
        problems.append(f"no section for {name}")
        continue
    for action in subparser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.option_strings:
            long = max(action.option_strings, key=len)
            if f"`{long}`" not in text and not re.search(r"`" + re.escape(long) + r"[ =][^`]*`", text):
                problems.append(f"{name}: option {long} not documented")
            if action.default not in (None, False, [], argparse.SUPPRESS):
                if f"`{action.default}`" not in text:
                    problems.append(f"{name}: default of {long} ({action.default}) not stated")
        else:
            if f"`{action.dest}`" not in text and f"`{action.metavar}`" not in text:
                problems.append(f"{name}: positional {action.dest} not documented")
        for choice in action.choices or ():
            if f"`{choice}`" not in text:
                problems.append(f"{name}: choice {choice} not listed")
    blocks = re.findall(r"```console\n(.*?)```", text, flags=re.S)
    commands = [line[2:] for block in blocks for line in block.splitlines() if line.startswith("$ ")]
    if not commands:
        problems.append(f"{name}: no console example")
    for command in commands:
        if not command.startswith("taskq "):
            problems.append(f"{name}: example does not start with '$ taskq ': {command}")
            continue
        argv = shlex.split(command)[1:]
        try:
            args = build_parser().parse_args(argv)
        except SystemExit:
            problems.append(f"{name}: invalid example: {command}")
            continue
        if args.command != name:
            problems.append(f"{name}: example runs another subcommand: {command}")
if "docs/CLI.md" not in pathlib.Path("/work/README.md").read_text(encoding="utf-8"):
    problems.append("README.md does not link docs/CLI.md")
for path in pathlib.Path("/work/taskq").glob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            if not ast.get_docstring(node):
                problems.append(f"{path.name}: {node.name} has no docstring")
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and not item.name.startswith("_") and not ast.get_docstring(item):
                    problems.append(f"{path.name}: {node.name}.{item.name} has no docstring")
for p in problems:
    print(p)
sys.exit(1 if problems else 0)
