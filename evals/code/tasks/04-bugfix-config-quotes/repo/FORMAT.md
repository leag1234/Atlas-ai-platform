# Configuration format

- The file is UTF-8 text, one entry per line: `key = value`.
- Blank lines and lines whose first non-space character is `#` are ignored.
- `key` is the text before the first `=`, stripped of spaces. An empty key is an error.
- A line without `=` is an error.
- Unquoted value: the text after `=` up to an optional `#` comment, stripped of spaces.
  It may be empty.
- Quoted value: after `=` and optional spaces, a value starting with `"` is quoted.
  Inside quotes, `\"` is a double quote, `\\` is a backslash, `\n` is a newline, and `#`
  is an ordinary character. Any other backslash sequence is an error.
  After the closing quote only spaces and an optional `#` comment may follow;
  anything else is an error. A missing closing quote is an error.
- If a key appears more than once, the last value wins.
- Errors raise `ParseError` whose `lineno` is the 1-based line number.
