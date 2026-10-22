# Deterministic document renderer spike

Task H implements REQ-ENG-004 and REQ-ENG-007: the model supplies structured
content; trusted code builds files. This library is not integrated into Atlas.

```python
from pathlib import Path
from packages.doc_render import Content, render

content = Content.model_validate({
    "title": "Project update",
    "sections": [{"heading": "Progress", "bullets": ["Review completed"]}],
})
result = render(content, Path("update.pdf"), image_root=Path("assets"))
assert result.pagination_verified
```

Install `packages/doc_render/requirements-dev.txt` into an isolated environment.
For a shared project environment, use a worktree-local `pip --target` directory
and add it to `PYTHONPATH`; do not modify the shared environment. Run:

```bash
ATLAS_SEARCH_PROVIDER=serpapi python -m unittest tests.test_doc_render -v
python -m tests.doc_render_bench output/deck.pptx --sections 10
python -m tests.doc_render_bench output/document.pdf --sections 40
```

The schema accepts a title, substantive sections, paragraphs, bullets, rectangular
tables and relative PNG/JPEG image paths contained by an explicit asset root.
Blank/control text, unknown fields, unsupported font characters and oversized
content fail explicitly. The caller owns output paths and asset permissions.

PPTX uses python-pptx, DOCX uses python-docx, XLSX uses openpyxl, and PDF uses
ReportLab with its bundled Vera font. All document text comes from the input.
Spreadsheet cells are literal strings, including formula-shaped values. PDF and
slides share measured layout, wrap and paginate overflow, and reject oversized
atomic rows. ZIP metadata and PDF identifiers are deterministic. Output replaces
the destination only after reopening and verification.

Verification counts real PDF pages and PPTX slides, requires text on every unit,
and checks supplied text. DOCX and XLSX reopen with document/worksheet checks;
**their physical pages are unverified**. `verify(..., require_pagination=True)`
rejects them. A separately generated PDF does not prove Office layout fidelity.
PDF font embedding is checked in tests; Office font substitution, tables across
page breaks and clipped Office content require an actual renderer. Raster/image
presence and readable text are necessary checks, not proof of semantic quality.

## ElixirOffice evaluation

Recommendation: **do not adopt it yet**. Revisit the native converter specifically
for Office-to-PDF rendering and verification, retaining structured Python builders.
[The WASM study](https://github.com/leag1234/elixiroffice/blob/5d95d64a7b3178ecac8a4e0c341064cdc09a602d/docs/wasm.md)
says headless Office-to-PDF/image needs native doctrenderer/sdkjs; WASM PDF writing
consumes an editor-produced drawing stream. Its writer is not a standalone Office
layout engine. The documented native entry point is `x2t task.xml`; the service
uses `POST /converter`. Neither was executed in this spike.

The beta1 image manifest contains 5,220,612,608 bytes of uncompressed layers;
normal containerd blob retention plus extraction could exceed the 8 GB task cap.
No image was pulled. LibreOffice is absent. ElixirOffice offline operation,
conversion latency, memory and fidelity are therefore **unmeasured**.

Five fresh Python processes per case measured median build plus verification:
10 slides 45.67 ms, 10-page PDF 26.98 ms, 40-page PDF 82.96 ms. Fresh process totals
were 486, 463 and 580 ms; peak RSS was below 95 MiB across the five workloads.
These are synthetic section-based documents, not dense reports or an Office
conversion comparison. All 50 generated PDF pages rasterized with text; French
accents, tables, lists and images are covered by offline tests.

[LICENSE](https://github.com/leag1234/elixiroffice/blob/5d95d64a7b3178ecac8a4e0c341064cdc09a602d/LICENSE)
and [NOTICE](https://github.com/leag1234/elixiroffice/blob/5d95d64a7b3178ecac8a4e0c341064cdc09a602d/NOTICE)
identify AGPL-3.0-only and component notices. Network deployment, linking,
redistribution and corresponding-source scope need owner legal review; process
separation is not a legal conclusion. No upstream implementation was copied.
