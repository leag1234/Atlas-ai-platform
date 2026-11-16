# 25. Reminders as calendar files

First step of reminders (owner request 2026-11-16, low priority). A person asks
Atlas to remind them of something at a time; Atlas answers with one sentence that
states the computed date and time, and a calendar file (`.ics`, RFC 5545) with an
alarm. Opened on a phone, the file adds an event to the calendar, which rings at the
time. No new menu entry: the "Automations" entry is a later roadmap item (1.1).

## Behaviour

- Only explicit requests are handled, in the five interface languages: "remind me
  tomorrow at 9 to call the plumber", "set a reminder for Friday 5pm", "erinnere mich
  Freitag um 18 Uhr", "recuérdame mañana a las 9", "ricordami domani alle 9", and the
  French verb forms (rappelle-moi, fais-moi penser, create-a-reminder phrasings).
  Typos in the key words are tolerated (one edit, Damerau-Levenshtein). The noun
  alone (a French sentence about a vaccine booster, "rappel") and requests for
  information ("remind me what RRULE means") go to the normal chat.
- Code decides the date and time (roadmap T8): no model call, no routing call, cost
  zero. The person's IANA zone comes from the interface (`ui_timezone`), then the
  location's zone; without either, Europe/Paris is used and named in the answer, as
  for the `date_time` tool.
- Relative times ("in 2 hours") are elapsed time; days and weeks keep the wall-clock
  time across a daylight-saving change. A wall time skipped by the change moves
  forward (2:30 becomes 3:30).
- Recurring reminders become an RRULE: daily, weekdays, given weekdays, every N hours
  or days, a day of the month, yearly.
- An early alarm ("15 minutes before", "an hour before", "the day before", in each
  language) adds a second DISPLAY alarm; the first one is at the event time.
- No time, a part of the day only ("tonight", "tomorrow morning"), a moment already
  past or an ambiguous numeric date in English ("3/4") get one short question; the
  next message completes the request.
- A second request in the same message is not dropped silently: the answer says that
  only the reminder was handled.
- The file is stored in the request's own user-file area, like every generated file,
  and downloaded through the same owner-checked route. Nothing leaves the host, so
  every privacy level, strict included, allows it.

## Survey (owner rule: existing projects first)

| Project | Licence | Maintenance | Verdict |
|---|---|---|---|
| dateparser 1.4.3 | BSD-3-Clause | active | Already installed (through trafilatura and htmldate). Measured on the reference phrasings: the French 16:15 request and "remind me tomorrow at 9" found no date; "Freitag um 18 Uhr" read as Wednesday 00:00; "mañana a las 9" and "domani alle 9" read as 10:00; a weekly French request read as today 18:00; no recurrence. Rejected for this use. |
| python-dateutil 2.9 | Apache-2.0 or BSD | active | Already a dependency. Its fuzzy parser reads English only; its `rrule` expands the written RRULE in the tests. |
| icalendar 7.3.0 | BSD-2-Clause | active (collective, release 2026-08-19) | Reference RFC 5545 library, pure Python, depends on python-dateutil and tzdata (Apache-2.0, data only). Added to `requirements-dev.txt` as the independent parser of the tests, including its VTIMEZONE reader. |
| ics 0.7.3 | Apache-2.0 | maintained (release 2026-04-15, 0.8 in development) | Not needed once icalendar parses the files in the tests. |

Choice: the grammar (`reminder_parse.py`) and the writer (`reminder_ics.py`) are code
of this repository, with no new runtime package. The writer is short (escaping,
75-octet folding, VTIMEZONE built from the same zoneinfo data, with yearly rules when
the transitions follow one), and the production host needs no new install. The tests
parse every file back with icalendar and compare the offsets its VTIMEZONE gives with
the zone database over ten years, for five zones in both hemispheres.

## Limits of this step

- Some calendar applications ignore the alarms of an imported file and apply their
  own default notification; the event itself is always added.
- A request that mixes a reminder with another question gets the reminder only.
- English numeric dates with both numbers up to 12 are asked again with the month in
  words.
