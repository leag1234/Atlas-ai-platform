# M24 — original owner acceptance (2026-09-18)

## M24 — Documents and files: read, produce, transform

Source: owner requirement of 2026-09-18, after M23/D8 revealed that attached documents were
never read at all. Rather than building an extractor, a generator and a download mechanism
from scratch, Open WebUI's own **Open Terminal** provides all three. It was installed and
measured on the VM on 2026-09-18 before this milestone was written.

### Verified facts (measured, not documentation)
| Point | Result |
|---|---|
| Container `ghcr.io/open-webui/open-terminal` on the VM | running, API 200 on `/docs` |
| Sovereignty | local container, local volume, no external service |
| Office rendering engine | LibreOffice headless (`/usr/bin/soffice`), image 5.71 GB |
| PPTX → PDF conversion | **2.0 s cold, 0.5 s warm** |
| Python libraries present | python-docx, openpyxl, python-pptx, pypdf, pandas, matplotlib |
| Open WebUI v0.11.3 support | **already supported** — `utils/terminals.py`, `TERMINAL_SERVER_CONNECTIONS` |
| Attachment routing | `TERMINAL_CHAT_UPLOAD_MODES = {'default', 'filesystem'}` |
No Open WebUI upgrade is needed, so the M23 CSS work is not at risk.

### D1 — Wire Open Terminal into the stack
- Start the container from `services/orchestrator/serving.py`, alongside the UI and the
  database, with a named volume for persistence and an API key read from `secrets.env`
  (`OPEN_TERMINAL_API_KEY`). Bind the port to `127.0.0.1` only.
- Configure `TERMINAL_SERVER_CONNECTIONS` in `infra/chat-ui.env` with the container URL
  (service name on the shared Docker network, not `localhost`) and the key.
- Set the connection's chat upload mode to **`filesystem`** so attachments reach the
  terminal intact instead of being reduced to RAG fragments by Open WebUI. This is the
  direct fix for the 2026-09-18 PDF failure.
- `make serve` starts everything; the owner runs one command, as in M20/D5.

### D2 — Containment
The terminal gives the model a shell. Even inside Docker, restrict what it can reach:
- no access to the VM's internal network (no route to 8010/8020/8030, the Scaleway keys,
  or the host filesystem);
- the ATLAS repository is **not** mounted into it;
- a dedicated volume for user files only;
- document in `runbooks/files.md` what the terminal can and cannot reach, and how to stop
  it. State plainly that a model with shell access is a new exposure surface for the PoC.

### D3 — Reading every format
With `filesystem` uploads, the model receives the file itself. It must handle: PDF, DOCX,
XLSX, PPTX, ODT, ODS, ODP, CSV, TXT, MD, and images. For spreadsheets, preserve the table
structure (columns, sheets) rather than flattening to prose. Report extraction failures
explicitly (scanned PDF without a text layer, password-protected file) instead of
silently returning fragments — the failure mode observed on 2026-09-18.

### D4 — Producing downloadable files
The model must be able to create a file and return it so the owner can download it.
Open WebUI renders a file card in the reply when asked to show the file in the chat.
Supported outputs: DOCX, XLSX, PPTX, PDF, CSV, MD, PNG (charts).
Conversion to PDF goes through `soffice` and costs ~0.5 s warm; keep that path warm or
tell the user when a conversion is running (the activity indicator from M23/D7).

### D5 — Transforming an existing file
Two strategies, and the model must SAY which one it used:
- **regenerate**: extract the content, produce a clean new document (loses the original
  formatting, appropriate when the request is "restructure with proper headings and a
  table of contents");
- **edit in place**: modify the file with python-docx/openpyxl (preserves styles, images,
  formulas; appropriate for a spelling correction or an added sheet).
Never silently destroy formatting the user did not ask to change.

### D6 — Finish M23: the CSS targets the wrong selectors
The M23 stylesheet is mounted and served correctly (6 510 bytes reach the browser) and is
well written — it even honours the user's `--app-font-family`. But it targets `.markdown`,
and Open WebUI 0.11.3 uses different classes. The real ones, read from the running build:
`copy-code-button`, `run-code-button`, `nb-code-content`, `nb-code-source`,
`nb-code-source-raw`, `file-code-editor`. Their generated Svelte suffixes
(`svelte-1wcdx53`) MUST NOT be hardcoded: they change at every build, so match on the
stable prefix only.
Required: inspect the DOM of a RENDERED answer (not the source bundle), rewrite the
selectors, and **verify in the browser that the rendering actually changed** before
declaring the work done. Add a before/after screenshot pair to `reports/M24.md`.
A stylesheet whose rules match nothing passes a static gate and changes nothing on screen:
that is exactly what happened in M23.

### D7 — Finish M23: the activity indicator never appears
Observed 2026-09-18 on a request with no tool call: the screen shows a static cursor for
the whole generation. The state may be emitted server-side but never rendered. Diagnose
end to end (emission, transport, rendering), fix, and prove it with a screenshot taken
during generation.

### D8 — Attachment cost ceiling, and Tavily for web search
- **Ceiling**: a real PDF reached 181 657 tokens — inside the 262 144-token window but
  above the 0.10 EUR limit, so the request was refused after 3.6 s while the extraction
  itself had worked. Raise the ceiling to **0.30 EUR for requests carrying an attachment**
  (0.10 EUR otherwise). Above 0.30 EUR, apply the M10 hierarchical synthesis rather than
  refusing. Keep the refusal message style: it stated the measured values against the
  limit and that no corpus retrieval was used, which is exactly right.
- **Search provider**: SerpApi's 250 free monthly searches were exhausted by a single
  corpus run on 2026-09-18, which silently degraded four measured cases
  (`search_unavailable` in the logs). Switch to **Tavily** (1 000 free searches/month,
  key in `secrets.env` as `TAVILY_API_KEY`, POST `https://api.tavily.com/search` with
  `api_key` in the body). Tavily returns the page CONTENT, not just links, so the separate
  `web_fetch` step disappears for search results — remove it from that path; this also
  removes the `http_404` and `loop_detected` errors seen in the logs. Keep SerpApi behind
  a provider switch as fallback. Verified on 2026-09-18: the CPC query reaches
  `cpctech.cpcwiki.de`, and the energy query returns arXiv and ScienceDirect with figures.
- When the search quota is exhausted or the provider fails, say so in the answer instead
  of falling back silently on model knowledge. Four corpus cases were scored on degraded
  searches without anyone noticing.

### Validation journeys

Original dated multilingual owner wording is preserved byte-for-byte inside
[tests/journeys/m24-cases.json](../tests/journeys/m24-cases.json), under
owner_validation_verbatim. These are binding acceptance cases:

- J42: downloadable /e/OS and Murena presentation plus PDF, using model knowledge
  and public sources only; no corpus retrieval, title and multiple content slides.
- J43: correct attached Word spelling and structure, headings and table of contents;
  return DOCX and state regeneration versus editing in place.
- J44: compare March purchases with the previous March; preserve the workbook,
  add a comparison sheet and embedded evolution chart with correct figures.
- J45: synthesize a PDF of at least20 pages; cover beginning, middle and end.
- J46: explicitly identify scanned/password-protected PDF failure.
- J47: synthesize approximately180000 tokens within0.30 EUR or use M10 hierarchy.
- J48: search CPC OUTI/OTIR timings through Tavily, cite CPC-specific evidence;
  explicitly disclose quota exhaustion instead of answering from memory.
- J49: screenshot actual serif prose and code without line numbers/default toolbar.

### Note on the model's role
The plumbing is proven; what is not yet known is whether the model drives the terminal
well — writing correct Python, handling its own errors, producing a clean file. J42–J44
measure exactly that, and the three configured models should be compared on them. Record
per journey: which model, how many terminal commands, how many failed, total seconds.

**Definition of done**: `make verify-m24` passes; J42–J49 pass; `runbooks/files.md`
documents the containment; `make serve` starts the terminal with everything else;
`reports/M24.md` carries the before/after screenshots proving the rendering and the
activity indicator changed on screen.

### M24 supersedes the read-only tool restriction (owner ruling, 2026-09-18)
`docs/13` POC-W3 restricted model tools to read-only. M24 requires a shell with write
access, so the two conflict. AUTHORIZED: for the Open Terminal path only, the read-only
restriction is SUPERSEDED by the containment described in M24/D2.

What is granted: executing Python and shell commands inside the Open Terminal Docker
container, and creating or modifying files in its dedicated volume.
What still holds, and is the condition of this authorisation:
- the container has no route to the VM's internal services (8010/8020/8030), no Scaleway
  credentials, and no mount of the ATLAS repository;
- its volume carries user files only, nothing of the platform;
- the port is bound to 127.0.0.1;
- `runbooks/files.md` states plainly what the terminal can and cannot reach, and how to
  stop it.
Read-only remains the rule for every OTHER tool path (web fetch, retrieval, MCP).
This is an explicit owner exception: apply it, do not stop on it again.
