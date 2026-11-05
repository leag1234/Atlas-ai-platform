import argparse
import json
import sys

sys.path.insert(0, sys.argv[1])
from taskq.cli import build_parser  # noqa: E402


def sig(parser):
    out = []
    for a in parser._actions:
        if isinstance(a, argparse._HelpAction):
            continue
        if isinstance(a, argparse._SubParsersAction):
            out.append({name: sig(p) for name, p in a.choices.items()})
            continue
        out.append([type(a).__name__, a.option_strings, a.dest, repr(a.default), list(a.choices or []),
                    getattr(a.type, "__name__", repr(a.type)), a.nargs, a.required, a.const])
    return out


print(json.dumps(sig(build_parser()), sort_keys=True))
