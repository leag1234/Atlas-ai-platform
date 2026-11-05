# taskq command reference

Global option: `--file` (default: `tasks.json`) selects the task file.

## `taskq add`

Adds a task. Positional `title`. Options: `--priority` (one of `low`, `normal`, `high`; default: `normal`),
`--tag` (repeatable).

```console
$ taskq add "write docs" --priority high --tag doc
```

## `taskq list`

Lists open tasks. Options: `--all` includes done tasks, `--tag` filters by tag, `--limit` (default: `20`).

```console
$ taskq list --all --limit 5
```

## `taskq done`

Marks the task `task_id` as done.

```console
$ taskq done 3
```

## `taskq export`

Exports all tasks. Options: `--format` (one of `json`, `csv`; default: `json`), `--output` (default: `-`, standard output).

```console
$ taskq export --format csv --output tasks.csv
```
