# M19 image-tool implementation

M19 D1/D3, REQ-ENG-004/009 and REQ-FIN-002. Chat and scoped project conversations
expose `generate_image(prompt)`; regex matching no longer gates generation.
The selected tool ends the model loop and generation receives only the remaining
request budget/deadline. Rewrite carries source and output language explicitly.

The public API regression failed before implementation (504 for a model-selected
natural drawing request). Tests cover image delivery, invalid/disabled arguments,
combined decision/image cost, exhausted budgets and rewrite reservation.
Real GPU recordings now cover the French and English drawing phrasings and the
combined web/image conversation. See [the M19 report](M19.md) for evidence and
remaining CI authority; local results alone do not establish completion.
