# Daily activity specification

Usage: `python3 daily_active.py FILE.csv` prints the document to standard output and exits 0.

## Input

UTF-8 CSV with header `timestamp,user_id,event`. A row is skipped (and counted in `skipped`) when it does not
have exactly 3 fields, when `user_id` or `event` is empty after stripping spaces, or when `timestamp` is not of
the form `YYYY-MM-DDTHH:MM:SS` immediately followed by `Z` or by an offset `+HH:MM` / `-HH:MM`, or is not a valid
date and time. Completely blank lines are ignored and not counted.

Normalisation: `user_id` and `event` are stripped and lowercased; the timestamp is converted to UTC. Two rows
with the same UTC instant, user and event are duplicates: only one of them counts (duplicates are not skipped
rows).

## Output

`json.dumps(document, indent=2)` followed by a newline, where `document` has exactly these keys in this order:

- `days`: one object per UTC calendar day from the first to the last day that has a counted row, inclusive,
  in date order, including days without activity. Each object is
  `{"date": "YYYY-MM-DD", "active_users": <distinct users that day>, "events": <counted rows that day>}`.
- `top_event`: `{"name": <event>, "count": <counted rows with that event>}` for the most frequent event, ties
  going to the alphabetically first name; `null` when nothing counts.
- `skipped`: number of skipped rows.

When nothing counts, `days` is an empty list.
