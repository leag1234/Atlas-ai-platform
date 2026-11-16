# Dates, times, units and currencies computed by code

Roadmap T8, item 4: the code does or checks, the model decides. A weekday, a
number of business days, a time in another zone, a unit conversion or a currency
conversion is computed by deterministic code and returned to the model with what
it needs to state the result (rate date, source, assumptions). The model never
computes these in its head.

## Tools

Three chat tools, declared in code (`services/orchestrator/exact_tools.py`), not
in `prompts/`:

| Tool | Module | What it computes |
|---|---|---|
| `date_time` | `datecalc.py` | current time in a zone (server clock), zone conversion with daylight saving (gaps refused, overlaps return both readings), date difference (days, weeks, calendar years/months/days), date plus or minus minutes, hours, days, weeks, months (clamped to month end), years or business days, weekday, ISO week, day of year, leap year, age, public holidays, business days between two dates (both ends included, optional Saturdays for French *jours ouvrables*) |
| `unit_convert` | `units.py` | length, mass, volume and cooking measures, area, speed, temperature, energy, power, pressure, data sizes (SI and binary prefixes, French octets), durations, fuel consumption (L/100 km to mpg is an inverse relation); ambiguous names (ton, cup in French, calorie) and incompatible dimensions are refused with a precise error |
| `currency_convert` | `fx_rates.py` | conversion with the ECB euro reference rates; cross rates go through the euro; the answer carries the rate, the rate date, the source name and the feed URL |

When the call names no zone, `date_time` uses the browser zone of the request
(`ui_timezone`), or Europe/Paris when the browser sent none.

## Offering the tools without breaking recorded replays

A tool schema is part of the request sent to the model, and recorded provider
replays match requests exactly (`tests/provider_recording.py`). Adding the tools
to every turn would change every recorded request. The tools are therefore
offered on the turns that are about dates, times, units or currencies
(`exact_tools.relevant`, the same pattern as the visual tools): patterns in the
five interface languages, tolerant of missing accents, plus a short follow-up
that inherits the subjects of the previous user message. No recorded question
matches these patterns, so every recorded request is unchanged, while the turns
that need the tools carry their full description and schema.

## Sources and truthfulness (D7)

The rate feed is fetched by the turn, so its URL is added to the turn's
retrieved sources (`Interaction.retrieved_urls`), exactly like a fetched page:
the answer may cite it, and the citation filter keeps it. An unreachable,
malformed or unexpected feed is an `exchange_rates_unavailable` or
`exchange_rates_invalid` error; no rate is ever estimated. When the person gives
their own rate, the tool description sends the model to `calculator`.

Strict privacy mode closes every third-party egress, so the rate download is
refused there with `strict_privacy_unavailable` and `currency_convert` is not
offered on strict turns (`packages.privacy.STRICT_EXCLUDED_TOOLS`); dates and
units are pure code and stay available.

## Survey of existing libraries (2026-11-14)

| Need | Candidates | Choice |
|---|---|---|
| Time zones | `zoneinfo` (standard library, IANA database), `pytz`, `dateutil.tz` | `zoneinfo`: standard, correct folds for daylight saving, already used across the services |
| Date arithmetic | `datetime`, `python-dateutil` `relativedelta`, `pendulum`, `arrow` | `datetime` plus a 10-line month addition: `dateutil` is only a transitive dependency here, and month clamping must be explicit in the answer |
| Public holidays | [`holidays`](https://github.com/vacanza/holidays) (MIT, releases every two weeks, hundreds of countries and subdivisions, pure Python, no network), `workalendar` (MIT, last release 17.0.0 in January 2023), `opening-hours-py` (already installed, has calendars but no names and no subdivisions) | `holidays` 0.107 (2026-10-19), pinned with `python-dateutil` 2.9.0.post0, pip-audit clean; covers France with Alsace and Moselle (6AE, 57) and overseas departments |
| Units | [`pint`](https://github.com/hgrecco/pint) 0.26.2 (BSD-3, released 2026-10-19), `astropy.units`, an in-house table | in-house table: `pint` brings four more packages, uses floats by default and English names only, so a French alias layer would be needed anyway; a decimal table of legal definitions (SI brochure, NIST SP 811, 1959 yard and pound agreement) is exact, small and auditable |
| Currency rates | [ECB `eurofxref-daily.xml`](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html) (free, public, European), commercial APIs | ECB feed: no key, no US provider; parsed with the standard XML parser on a bounded body that may not declare a document type |

## Relative dates (2026-11-16)

Staging answered "il fait quel temps demain a Bayeux" on Monday 16 November with
"demain lundi 16 novembre 2026". A chat turn had no date in its context: the
`date_time` tool is offered on calendar questions only, "demain" is not one, and
the model read today from a search result's `consulted_at` and guessed tomorrow.
`services/orchestrator/relative_dates.py` closes the class in two deterministic
steps; nothing goes into `prompts/`.

- Calendar in context. A question that names a relative day (today, tomorrow,
  yesterday, the day after, a weekday, the weekend, this or next week, in N days,
  N days ago), or a short follow-up of one, gets the calendar around today
  appended to the system context: today and the time in the person's zone, two
  days back to thirteen days ahead with English weekday and ISO date plus the
  date in the answer language (CLDR through Babel), this and next weekend, this
  and next week. The cue reads the relative words of the five interface
  languages and a few more (nl, pt, pl, sv, da, ca, tr, ru, zh, ja), one-letter
  typos of the long ones, and the weekday names of every CLDR language. Other
  turns send the request they always sent, so recorded replays stay valid.
- Answer check. Every chat answer, streamed and stored, goes through `repair`. A
  relative day word followed by a date or a weekday ("demain lundi 16 novembre",
  "Tomorrow is Monday") must name the day code computes; a near miss (within
  seven days, or the right day with a wrong year) is rewritten in the form the
  answer used (weekday and month names from CLDR in the answer language,
  abbreviations, capitals, ordinal suffixes). A weekday written with a date must
  be that date's weekday: when the person named that weekday (or the weekend),
  the date is moved to it; otherwise the weekday is corrected. Dates further from
  today, without a year, are left alone, as are quotations of other days ("hier
  14 juillet 1789"), "esta mañana" and "heute Morgen" (morning), and lists such as
  "demain, lundi et mardi". The stream holds back only text that may still
  become a date phrase, so the streamed and the stored answers are identical.

Survey: hosted assistants put the current date in their system prompt (their
published system prompts show it); a window of resolved neighbouring days goes
one step further, and a deterministic post-check is the T8 rule applied to the
answer. `dateparser` (already installed through
trafilatura) parses relative expressions in many languages, but was rejected for
reminders on the reference phrasings (docs/25-reminders.md) and does not check
an answer. Babel ships CLDR weekday and month names for every language but not
CLDR's relative day fields, so those words are a fixed list.

Limits: relative words outside the listed languages are cues only; dates
written without a month name (17/11, 11月17日) are not checked; "tomorrow" is the
person's tomorrow, also for a place in another time zone.

## Caching

The feed is kept in memory for the Paris calendar day. A copy fetched before
16:15 Paris time is refreshed once that time has passed, since the ECB publishes
around 16:00 CET on TARGET working days. Weekends and TARGET holidays keep the
last published date, which the answer states.

## Limits and next steps

- Historical rates (a given past date) are not supported yet; the ECB also
  publishes 90-day and full-history files.
- Work turns do not share the chat tool layer (their tool groups come from
  `work_tool_selection.py`); adding these tools there is a separate change.
- The relevance patterns decide whether the model sees the tools. A phrasing they
  miss leaves the model without them; misses found in the corpus should be added
  as patterns, by class of phrasing.
