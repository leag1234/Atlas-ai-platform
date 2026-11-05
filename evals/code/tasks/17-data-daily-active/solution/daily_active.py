import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})")


def parse_ts(text):
    if not STAMP.fullmatch(text):
        return None
    try:
        value = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return value.astimezone(timezone.utc)


def main(argv):
    with open(argv[0], newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    skipped, seen = 0, set()
    for row in rows[1:]:
        if not row:
            continue
        if len(row) != 3:
            skipped += 1
            continue
        ts, user, event = row[0], row[1].strip().lower(), row[2].strip().lower()
        when = parse_ts(ts)
        if when is None or not user or not event:
            skipped += 1
            continue
        seen.add((when, user, event))
    users, counts, events = defaultdict(set), Counter(), Counter()
    for when, user, event in seen:
        day = when.date()
        users[day].add(user)
        counts[day] += 1
        events[event] += 1
    days = []
    if counts:
        day, last = min(counts), max(counts)
        while day <= last:
            days.append({"date": day.isoformat(), "active_users": len(users[day]), "events": counts[day]})
            day += timedelta(days=1)
    top = None
    if events:
        name, count = sorted(events.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        top = {"name": name, "count": count}
    sys.stdout.write(json.dumps({"days": days, "top_event": top, "skipped": skipped}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
