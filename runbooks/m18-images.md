# M18 image fidelity and conversation integrity

Source: MISSION M18 D1–D6; contracts/m18.md; REQ-ENG-004/009, REQ-FIN-002.

The image model remains the gateway's configured Apache-2.0 model. The alternative
non-commercial, gated weights were rejected for this PoC. See the upstream
[model and licence table](https://github.com/black-forest-labs/flux).
No claim about permission for commercial use of alternative weights is made.

The worker uses 1024px output, a random reproducible seed, a 512-token text limit
and a 180-second watchdog. Inputs above that token limit are rejected explicitly.
Guidance stays zero. The upstream distilled model is designed for one to four
steps; the matched-seed four/eight comparison retained four steps at equal observed
quality and lower latency. See [the measurements](../reports/m18/measurement.md).

The gateway rewrites the original request into English subjects, attributes, scene
and style, sharing the EUR0.05 reservation with GPU generation. Request deadlines
remain 120 seconds; the watchdog is a process fail-safe, not an extended user budget.
Original and rewritten prompts, image reference and seed enter the interaction log.
Generated PNGs live in BRAIN/generated-images and are served by opaque references.
Never paste image bytes into text context. Old inline assistant images are scrubbed
before text validation. Image iteration generates a new image; pixel editing is not
supported and receives an explicit localized response.

Follow-ups transform scoped previous answers with tools disabled. Status labels use
ui_locale (default fr), independent of the requested answer language. A transient
search failure retries with a different query in the same tool ledger.
