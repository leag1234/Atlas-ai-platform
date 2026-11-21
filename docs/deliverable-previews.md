# Deliverable previews

A file that Atlas publishes in a project (DOCX, PPTX, XLSX, PDF) is linked in
the answer with a download link and a preview link. Until November 2026 the
preview was the text the verifier read back from the file, served as
`text/plain`: a dump without layout, tables, pictures or slides. This note
records how other open projects preview office documents, and what Atlas does
now.

## Survey (November 2026)

| Project | How it previews | Licence | Self-hosting | Notes |
|---|---|---|---|---|
| LibreOffice headless (`soffice --convert-to`) | Converts DOCX, PPTX, XLSX (and ODF, legacy formats) to PDF or PNG | MPL-2.0 | Distribution packages on every Linux | The engine behind most entries below. One process per conversion with its own profile directory (`-env:UserInstallation`). |
| unoserver (unoconv successor) | Keeps one LibreOffice listener, clients send conversions over a local port | MIT | pip package plus LibreOffice | Saves the LibreOffice start-up per conversion. Its README warns the ports have no authentication and must stay internal; it does not restart a crashed LibreOffice. |
| Gotenberg | HTTP API in a Docker image: LibreOffice for office files, Chromium for HTML | MIT | One container | Used by Paperless-ngx (with Apache Tika) for office documents. A whole service to run and secure for a few conversions a day. |
| Nextcloud previews | Office preview providers call a local LibreOffice; newer versions also offer Imaginary or Collabora | AGPL-3.0 | Part of the server | ownCloud disabled office and PDF previews by default over security concerns; administrators enable them explicitly. |
| Collabora Online / Nextcloud Office | Full online editor; also has a convert-to and thumbnail endpoint | MPL-2.0 | Docker image, WOPI host | Faithful, but an editor server is far heavier than a preview needs. |
| ONLYOFFICE Docs | Document server with a conversion API that can produce thumbnails | AGPL-3.0 | Docker image | Same weight as Collabora; its own rendering engine. |
| poppler `pdftoppm` / `pdfinfo` | Rasterises PDF pages to PNG or JPEG | GPL-2.0 or later | Distribution package `poppler-utils` | Small, fast, already used by the files runbook. |
| pypdfium2 (PDFium) | Python binding of Chrome's PDF engine, rasterises pages | Apache-2.0 or BSD-3-Clause (PDFium: BSD-style) | Binary wheels | A good alternative to poppler; not in the environment today. |
| pdf.js | Renders PDF in the browser | Apache-2.0 | Static files (already vendored for the side view) | Needs JavaScript and a PDF: the preview page is meant to run without script. |

Security of converting untrusted files: LibreOffice and poppler parse complex
formats and have had vulnerabilities, including documents that fetch linked
content or run macros. The projects above isolate the converter (Collabora
and ONLYOFFICE in containers, Nextcloud disabled the providers by default,
unoserver keeps the listener local). Atlas deliverables are written by Atlas,
but their content comes from model output and user sources, so the converter
is treated as handling hostile input.

Sources: unoserver README (github.com/unoconv/unoserver), Gotenberg README
(github.com/gotenberg/gotenberg), Paperless-ngx configuration documentation
(Tika and Gotenberg endpoints), Nextcloud admin manual "Previews
configuration" and forum threads on office previews, pypdfium2 README
(github.com/pypdfium2-team/pypdfium2).

## Choice

LibreOffice headless, one process per conversion, then poppler. A conversion
measured 0.6 to 0.8 s for a six-page DOCX, a six-slide PPTX and a nine-page
XLSX on the build server, so a persistent listener (unoserver) or a separate
service (Gotenberg) would add an unauthenticated port or a container to run
for little gain. Both tools are distribution packages that an installing
organisation runs on its own servers; nothing is a hosted service.

Rendering (`services/retrieval/deliverable_render.py`):

- The input is the verified artifact bytes read back from the project store,
  checked against their SHA-256. No picture or page supplied by the caller is
  ever shown.
- A fixed Python child runs inside a new network namespace (`unshare --net`,
  or with a user namespace when not root). Without network isolation the
  renderer does not run. As defence in depth, a package with an external
  relationship (other than a hyperlink), an include or link field, or a data
  connection is not converted.
- The child has resource limits (90 s CPU, 1.5 GiB address space, 64 MiB per
  written file, no core file), its own temporary directory used as LibreOffice
  profile, HOME and TMPDIR, and is started in its own session: at the deadline
  the whole process group, LibreOffice included, is killed. The temporary
  directory is removed in every case.
- One rendering at a time per service process.
- poppler rasterises at most 60 pages as progressive JPEG, 1280 pixels wide;
  when the pictures exceed the 4 MiB page budget it retries at 1024 then 800
  pixels with lower quality, then keeps the pages that fit and says so.

The gallery page has no script and no words: the file name, the page or slide
count, `3 / 12` counters and a download chip named by the format (`DOCX`). It
is stored once per deliverable, so it does not depend on the reader's
interface language. Slides are a horizontal carousel with scroll snapping and
previous/next anchors on wide screens and a vertical list on a portrait phone;
documents are a vertical list of pages. Light and dark follow
`prefers-color-scheme`. It is served with

```
Content-Security-Policy: default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-downloads
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

The browser proxy shows HTML from project storage only as an attachment; the
deliverable preview route is the one exception, and the proxy sets the policy
above itself instead of trusting the upstream header.

## When the rendering runs

Not inside the publication: the publish tool keeps 20 s for the publication,
which already reopens and verifies the file, and every project call of an
instance shares one database connection, so a conversion must never run inside
a transaction.

- After the publication commits, a background task reads the bytes in one
  short transaction, renders outside any transaction, and stores the page in
  another (a `derived` resource whose identifier is derived from the
  deliverable, retention class E, counted in the derived and stored bytes
  quotas). The answer is still being written at that time, so the gallery is
  usually ready before the link is opened.
- A preview request that finds no gallery (opened very early, a deliverable
  published before this change, a lost background task) renders it on the
  spot within 25 s and serves it. It is stored when the reader may write in
  the project (owner, editor); a viewer gets the page without storing it.
  Concurrent requests for one deliverable wait for the rendering in progress.
- A failure (LibreOffice absent, no isolation, refused package, timeout,
  oversized) keeps the text preview and is not retried for 15 minutes. It never
  fails a publication.

## In the chat

The preview link opens the side view (the whole screen on a phone) with the
gallery in a frame sandboxed to `allow-downloads`; the side view's download
button fetches the file. A modified click (new tab) keeps the browser's
behaviour and shows the same page full size.

## Installation

`libreoffice-writer`, `libreoffice-calc`, `libreoffice-impress` (with
`--no-install-recommends`) and `poppler-utils`, plus `util-linux` for
`unshare`. Without them previews stay text.
