The `taskq` command-line tool has no user documentation. Write it:

1. Create `docs/CLI.md` with one level-2 heading per subcommand, written exactly as `` ## `taskq <subcommand>` ``.
   Under each heading, document every option and positional argument of that subcommand: mention each option by
   its long form in backticks (for example `` `--priority` ``), each positional argument by its name in backticks,
   and for every option that has a default value other than `None`/`False`, state the default in backticks
   (for example "default: `normal`"). For options with a fixed set of choices, list every choice in backticks.
2. Each subcommand section must contain at least one example in a fenced `console` code block, where every
   command line starts with `$ taskq ` and is a valid invocation of that subcommand.
3. Link `docs/CLI.md` from `README.md`.
4. Give every public function and class in `taskq/` (names not starting with `_`) a docstring.

Do not change the behaviour of the tool (the existing tests must pass, and the argument parser must stay the same).
