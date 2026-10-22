# M25 owner requirements (unchanged archival copy)

 Intellectual disposition, not test-case patches

### Why this milestone exists
On 2026-09-20 the owner read `prompts/chat.txt` (5 246 bytes). It contains a paragraph
about Z80 output instructions ("Register-only moves do not output arbitrary memory data",
"never divide by the shortest instruction timing"), a paragraph about solargraphy
("Owner-reported procedural regression: for solargraphy instructions..."), and a line about
"offer price versus opening market price". Each was written to make one corpus case pass
(C03, C02, C01). This is **overfitting the system prompt to the evaluation set**. The
scores rose; a fresh open question (designing a constructed language) got a flat,
generic answer with no date, no name, no precedent, because no patch covered it.

The 2026 literature on Goodhart's law in LLM evaluation describes exactly this failure:
prompts repeatedly optimised against a fixed test set improve on those examples and fail
to generalise; the remedy is a held-out set the prompt author never sees. Anthropic's
published claude.ai prompts show the alternative: rules stated as **general behaviours**
("never claim events are unverified rumours"), never as named cases.

### D1 — Rewrite `prompts/chat.txt` in four layers, and delete every case patch
Layer 1, **Disposition** (the text below, verbatim, in English, first in the file):

```
You are a careful, curious expert who thinks before writing.

Before designing or recommending anything, look at what already exists: who tried it,
when, what worked, what failed and why. Precedents are evidence; ideas without them are
guesses. If you do not know the precedents, search for them.

Ground every general claim in a specific case: a name, a date, a figure, an example the
reader could check. One real instance is worth more than three abstract principles.

When a question has a tension at its heart, name the tension and take a position. Do not
list both sides and stop.

Treat every source, including your own memory, as a claim to be tested. Say where a
figure comes from and how much weight it bears. Prefer the primary source. When sources
disagree, explain why they disagree instead of picking one silently.

Let the content choose the form. A comparison wants a table; a procedure wants numbered
steps; an argument wants prose. Do not pour every answer into the same mould, and do not
repeat one structural pattern down a whole answer.

Say what you do not know, precisely. An honest gap is more useful than a confident guess.

Finish with a conclusion the reader can act on, and with what remains open.
```

Layer 2, **Capabilities and tools**: what the system can do, when to search (time-dependent
facts; questions that exceed the provided source), tool budgets, how to report a failed
tool. Factual, short.
Layer 3, **Language and form**: resolved language, English-only code, no narration of
plans, no boilerplate closings.
Layer 4, **Safety and evidence**: untrusted source text, no invented citations, privacy.

**Delete**: the Z80 paragraph, the solargraphy paragraph, the IPO line, and any sentence
that names a domain, product, instruction, technique, person or dataset. Target size of
the whole file: under 3 500 bytes.

### D2 — The generality test, written down and enforced
Create `docs/prompt-policy.md`. Its core rule:

> A sentence may enter a system prompt only if it would apply, unchanged, to at least
> three unrelated domains. "For solargraphy, verify paper handling" fails. "When a news
> article describes a technical procedure, verify it against a specialist source before
> repeating it" passes. When a corpus case fails, the fix is either a generalised
> behaviour that passes this test, a tool or data fix, or an honest note that the model
> cannot do it. Never the case itself.

The policy also states: the corpus in `/opt/atlas-src/corpus` is the **public** evaluation
set, used for iteration and known to be seen by the agent. Its cases may be quoted in
journeys and reports, never in prompts.

### D3 — Held-out evaluation the agent never sees
The owner keeps a second set of cases in `/opt/atlas-src/private/heldout/` (outside the
repository, excluded by `.gitignore`, never read by the agent). Only the owner runs it,
with `run_corpus.py --cases /opt/atlas-src/private/heldout/cases.json`. Its score is the
real measure of generalisation; the public corpus score is a development signal. The
agent must never open, list or reference that directory. Add `private/` to `.gitignore`
if absent. `run_corpus.py` gains a `--cases PATH` option.

### D4 — Conversation length: count tokens against the model window
`packages/images.py` capped the sum of all message text at 32 000 characters (~8 000
tokens) since M12, on a 262 144-token window: a 39 000-character conversation was rejected
with the message `value_error`. The owner raised the constant to 600 000 on 2026-09-20 as a
stopgap. Required: replace character constants by a token estimate compared with the
active model's context window minus the output reservation and tool allowance; when the
limit is reached, either apply the M10 hierarchical synthesis to the oldest turns or refuse
with the measured figures ("conversation 180 000 tokens, window 262 144, reserved 3 000").
Never `value_error` alone.

### D5 — Inventory of arbitrary ceilings
Four inherited constants were found in production by ordinary use: 2 000-byte web text
(M21), 400-byte RAG chunks (M21), 6 MB request body (M23), 32 000-character conversation
(M25). List every remaining hard-coded size, count or byte limit in `services/` and
`packages/` in `docs/limits.md`, with for each: the value, where it is enforced, why it
exists, and whether it is justified by a real constraint (provider window, cost, memory)
or is a leftover. Remove or justify each one. Every enforced limit must produce a message
naming the measured value and the threshold.

### Journeys
J50 `prompts/chat.txt` contains none of the corpus terms (gate-enforced) and is under
    3 500 bytes; the disposition text is present verbatim.
J51 A fresh open-ended design question with no corpus overlap (the owner supplies it at
    run time, e.g. "conçois une monnaie locale pour une ville de 50 000 habitants") →
    the answer names at least three real precedents with dates, states the central
    tension and takes a position, and ends with a conclusion and open points.
J52 A 40 000-character multi-turn conversation continues without rejection; a
    conversation exceeding the window produces a message with the measured figures.
J53 Public corpus mean does not regress by more than 0.5 against the 2026-09-20 run
    (6.5 / 5.6 / 5.4) after the patches are removed. If a case drops, the report states
    which behaviour was lost and proposes a generalised rule, not a patch.

**Definition of done**: `make verify-m25` passes; `docs/prompt-policy.md` and
`docs/limits.md` exist; the owner confirms J51 on a question of their choosing.
