# Presentation design (PPTX)

Status: implemented in `packages/doc_render/deck.py` (2026-11-20).

## Problem

A deck asked of Atlas for investors (staging 2026-11-20) came out as nine white
slides, each a title over three to six default bullets, with no theme, colour,
shape, picture, chart, slide number or speaker note. The outline contract had no
place for key numbers or charts, and `notes` were printed on the slide. The
content followed no genre: an investor deck had no market, traction, team or ask,
and the model filled gaps with invented figures.

## Survey of existing open projects

| Project | Licence | What it offers | Fit for Atlas |
| --- | --- | --- | --- |
| python-pptx | MIT | Native PPTX: shapes, text frames, tables, native charts, notes slides | Already a dependency; Python only; editable output |
| PptxGenJS | MIT | JavaScript PPTX writer with masters and charts | Same output as python-pptx, adds a Node runtime to the renderer |
| Marp / Marp CLI | MIT | Markdown to HTML, PDF and PPTX | PPTX holds each slide as a picture; the editable mode is experimental and needs LibreOffice; needs Chromium |
| Slidev | MIT | Vue-based slides from Markdown | Web-first; PPTX export is pictures of slides |
| reveal.js | MIT | HTML slides | No PPTX; a good model for a web preview, not for the file |
| pandoc (and Quarto, which uses it) | GPL-2.0-or-later (pandoc), MIT (Quarto) | Markdown to PPTX through a reference template | Four layouts (title, content, two contents, comparison), no chart, no callout |
| LibreOffice Impress | MPL-2.0 | Masters and templates, headless conversion | Used to render and check decks (battery, previews), not to write them |

All of them are self-hostable and none sends content anywhere. The projects
that write real, editable PPTX shapes are python-pptx and PptxGenJS; the
Markdown tools trade editability for convenience. Their common design lessons,
kept here: a small closed set of layouts chosen per slide, a theme as a few
tokens (dark, accent, tint, text, muted), one face, a frame repeated on every
content slide (title, rule, footer, number), and the layout following the
content rather than a template name.

The genre structure follows the widely used pitch-deck outline (problem,
solution, product, market, business model, traction, team, competition,
roadmap, ask) and the usual structure of course and status decks.

## Choices

1. **python-pptx, code-built theme.** No new runtime. Every element is a native,
   editable shape, table or chart. No template file with boilerplate text: the
   repository rule "no templates containing user-visible boilerplate" holds.
2. **The model decides content, code decides layout (T8).** The outline gains
   optional `metrics` (2 to 4 value and label pairs), `chart` (labels, values,
   unit) and keeps `notes` (now speaker notes). Code picks the layout from what a
   slide holds: cover (first slide with one to three short lines), closing
   (short last slide), statement (one line), cards (two to four developed points
   or `Label: text` lines), pillars (two to four short points), content (list,
   with a chart, table or picture beside or under it), number callouts above.
   Two consecutive point slides never share a layout.
3. **Theme.** Five palettes (dark, accent, tint), picked from a hash of the deck
   title: stable across rebuilds, varied across decks, no organisation's colours
   by default. Face: Arial in the file (its metric twin Liberation in LibreOffice); text is
   fitted with the wider Vera metrics, so what fits here fits there.
4. **Nothing invented on a slide.** The footer repeats the deck title, the
   numbers are slide numbers, card numbers are ordinals. Chart values come from
   the outline; the `publish_document` description says what code does with each
   field, what each kind of deck covers, and that a fact not given nor read is
   written as unknown or to be confirmed. The older chat-only outline instruction
   (`presentation_delivery.py`) is unchanged: its M24 recording (J42) replays it.
5. **Other formats keep everything.** `Content.flattened()` turns notes into
   paragraphs, metrics into `label: value` bullets and a chart into the table of
   its numbers for DOCX, PDF and XLSX.
6. **Verification.** `verify()` reads chart categories and speaker notes, so the
   requested-text check covers them. Long tables continue on further slides with
   their header.

## Measurement

Six requests (French and English: investor deck, course, project status,
product launch, a three-slide request with a typo, a terse startup pitch), one
outline each from `qwen3.5-397b-a17b` with the previous and the new instruction,
rendered with the previous and the new builder, then with LibreOffice headless.
Before: white slides, title and bullets, notes printed on slides, invented
margins in the investor deck. After: themed decks with varied layouts, callouts
and charts where numbers were given, genre structure (12 slides for the
investor deck), "to be confirmed" where a figure was unknown. The images are kept
outside the repository (owner evidence folder).

## Not done here

- An organisation's own `.potx` template as the theme source.
- Pictures from the CPU image server on cover slides (the outline already takes
  `make_pictures` references; the model decides when one helps).
- The PDF form of a deck (`slides=True`) still uses the page layout of
  `layout.py`, not this theme.
