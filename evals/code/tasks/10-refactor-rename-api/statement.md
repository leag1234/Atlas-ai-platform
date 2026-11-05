The `textkit` package exposes camelCase functions, against our style guide. Rename them across the whole
repository:

| old | new |
|---|---|
| `toSnake` | `to_snake` |
| `wordCount` | `word_count` |
| `stripAccents` | `strip_accents` |
| `truncateWords` | `truncate_words` |

- The new names are importable from the same modules as before and from `textkit`.
- The old names stay importable from the same places for one release, as thin aliases that emit a
  `DeprecationWarning` (message mentioning the new name) on every call and then behave exactly like the new
  function.
- No code inside the package or the tests may call the old names any more (the CLI must run cleanly with
  `python3 -W error::DeprecationWarning -m textkit.cli ...`).
- Behaviour of every function is unchanged.
