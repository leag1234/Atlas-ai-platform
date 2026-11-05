import argparse
import csv
import json
import sys

from taskq.store import PRIORITIES, Store, sort_key


def build_parser():
    """Return the argument parser of the taskq command."""
    parser = argparse.ArgumentParser(prog="taskq")
    parser.add_argument("--file", default="tasks.json")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a task")
    add.add_argument("title")
    add.add_argument("--priority", choices=PRIORITIES, default="normal")
    add.add_argument("--tag", action="append", default=[], dest="tags")

    lst = sub.add_parser("list", help="list tasks")
    lst.add_argument("--all", action="store_true", help="include done tasks")
    lst.add_argument("--tag")
    lst.add_argument("--limit", type=int, default=20)

    done = sub.add_parser("done", help="mark a task as done")
    done.add_argument("task_id", type=int)

    export = sub.add_parser("export", help="export tasks")
    export.add_argument("--format", choices=("json", "csv"), default="json")
    export.add_argument("--output", default="-")
    return parser


def render_list(tasks):
    """Format tasks as list lines."""
    return "".join(f"{t.id:>4} [{'x' if t.done else ' '}] {t.priority:<6} {t.title}\n" for t in tasks)


def main(argv=None):
    """Run the command line and return the exit status."""
    args = build_parser().parse_args(argv)
    store = Store(args.file)
    if args.command == "add":
        task = store.add(args.title, args.priority, args.tags)
        print(f"added {task.id}")
    elif args.command == "list":
        tasks = [t for t in store.load() if (args.all or not t.done) and (not args.tag or args.tag in t.tags)]
        sys.stdout.write(render_list(sorted(tasks, key=sort_key)[: args.limit]))
    elif args.command == "done":
        try:
            store.complete(args.task_id)
        except KeyError:
            print(f"no task {args.task_id}", file=sys.stderr)
            return 1
    elif args.command == "export":
        tasks = store.load()
        out = sys.stdout if args.output == "-" else open(args.output, "w", newline="", encoding="utf-8")
        if args.format == "json":
            json.dump([t.__dict__ | {"tags": list(t.tags)} for t in tasks], out, indent=2)
            out.write("\n")
        else:
            writer = csv.writer(out)
            writer.writerow(["id", "title", "priority", "tags", "done"])
            for t in tasks:
                writer.writerow([t.id, t.title, t.priority, ";".join(t.tags), t.done])
        if out is not sys.stdout:
            out.close()
    return 0
