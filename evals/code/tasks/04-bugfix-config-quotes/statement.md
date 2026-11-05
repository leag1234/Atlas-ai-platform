The `confparse` parser breaks on quoted values. For example the line
`motd = "Welcome to #general \"team\""   # shown at login` is parsed as `"Welcome to` instead of
`Welcome to #general "team"`, and an unterminated quote is silently accepted.

Make `confparse.parse` implement the format described in `FORMAT.md` exactly, including the error cases.
Keep `ParseError` and its `lineno` attribute.
