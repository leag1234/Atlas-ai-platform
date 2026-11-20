"""Faithful previews of published documents: their pages and slides as pictures.

A published DOCX, PPTX, XLSX or PDF used to preview as the text its verifier
read back, a plain text dump. The server now derives the preview itself from
the verified artifact bytes (never from a caller-supplied picture): LibreOffice
headless converts the package to PDF, poppler rasterises the pages, and a
static HTML page shows them (a swipe carousel for slides, a page list for
documents). The page has no script and is served under GALLERY_CSP.

Rendering runs after the publication commits, outside any database
transaction, in a fixed child process with no network namespace, a private
temporary profile, resource limits and a deadline that kills its whole process
group. A rendering that fails or is unavailable (LibreOffice absent, no network
isolation, timeout, oversized) leaves the text preview in place; it never fails
a publication. Design note: docs/deliverable-previews.md.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

RENDER_VERSION = "deliverable-render-1"
RENDER_SUFFIXES = (".docx", ".pptx", ".xlsx", ".pdf")
# Bounds of one gallery: pages shown and bytes of the whole HTML page.
MAX_PAGES = 60
RENDER_LIMIT = 4 * 1024 * 1024
# Deadlines: the background rendering after publication, and the lazy one a
# preview request may wait for (the browser proxy reads for 35 s).
BACKGROUND_SECONDS = 60.0
REQUEST_SECONDS = 25.0
# A failed rendering is not retried on every preview request.
FAILURE_SECONDS = 900.0
# No script, no network, no form, no plugin; only inline pictures and styles.
# sandbox gives the page an opaque origin; allow-downloads keeps its link to
# the file working.
GALLERY_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
    "sandbox allow-downloads"
)

# This fixed child receives document bytes only, with no service credentials,
# inside its own network namespace. Its limits are inherited by LibreOffice
# and poppler; the parent kills the whole process group at the deadline.
RENDER_PROGRAM = r"""
import base64, io, json, re, resource, subprocess, sys, zipfile
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
resource.setrlimit(resource.RLIMIT_AS, (1610612736, 1610612736))
resource.setrlimit(resource.RLIMIT_FSIZE, (67108864, 67108864))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
config = json.loads(sys.argv[1])
work = Path(config["work"])
raw = sys.stdin.buffer.read(16777217)
if not 0 < len(raw) <= 16777216:
    raise SystemExit("artifact_size")
suffix = config["suffix"]
if suffix != ".pdf":
    from packages.attachment_reader import validate_package
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        validate_package(archive)
        # Defence in depth behind the network namespace: a package that links
        # outside itself (pictures, OLE, data connections, include fields) is
        # not converted. Hyperlinks are only written, never fetched.
        field = re.compile(r"(?:instr=\"|<w:instrText[^>]*>)\s*(?:INCLUDEPICTURE|INCLUDETEXT|LINK|DDE|DDEAUTO|IMPORT)\b")
        for name in archive.namelist():
            lowered = name.lower()
            if lowered == "xl/connections.xml":
                raise SystemExit("external_data")
            if not lowered.endswith((".rels", ".xml")):
                continue
            part = archive.read(name).decode("utf-8", "replace")
            for match in re.finditer(r"<Relationship\b[^>]*>", part):
                if re.search(r"TargetMode\s*=\s*[\"']External", match[0]) and not re.search(r"Type\s*=\s*[\"'][^\"']*/hyperlink[\"']", match[0]):
                    raise SystemExit("external_reference")
            if field.search(part):
                raise SystemExit("external_field")
source = work / ("artifact" + suffix)
source.write_bytes(raw)
pdf = source
if suffix != ".pdf":
    out = work / "pdf"
    out.mkdir()
    subprocess.run(
        [config["soffice"], "-env:UserInstallation=" + (work / "profile").as_uri(),
         "--headless", "--invisible", "--nologo", "--norestore", "--nolockcheck",
         "--nodefault", "--convert-to", "pdf", "--outdir", str(out), str(source)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    pdf = out / "artifact.pdf"
    if not pdf.is_file():
        raise SystemExit("conversion_failed")
info = subprocess.run([config["pdfinfo"], str(pdf)], capture_output=True, check=True).stdout.decode("utf-8", "replace")
pages = int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1])
size = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+)", info, re.M)
landscape = suffix == ".pptx" or (size is not None and float(size[1]) > float(size[2]))
if pages < 1:
    raise SystemExit("no_pages")
shown = min(pages, config["max_pages"])
from PIL import Image
result = None
for width, quality in ((1280, 80), (1024, 70), (800, 60)):
    directory = work / ("pages-%d" % width)
    directory.mkdir()
    subprocess.run(
        [config["pdftoppm"], "-f", "1", "-l", str(shown), "-scale-to-x", str(width), "-scale-to-y", "-1",
         "-jpeg", "-jpegopt", "quality=%d,optimize=y,progressive=y" % quality, str(pdf), str(directory / "page")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    files = sorted(directory.glob("page-*.jpg"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    if len(files) != shown:
        raise SystemExit("rasterisation_failed")
    images = []
    for path in files:
        with Image.open(path) as picture:
            picture.verify()
        with Image.open(path) as picture:
            images.append((path.read_bytes(), picture.size))
    result = images
    if sum(len(data) for data, _ in images) * 4 // 3 <= config["budget"]:
        break
total = 0
kept = []
for data, (w, h) in result:
    total += len(data) * 4 // 3 + 200
    if total > config["budget"]:
        break
    kept.append(dict(jpeg=base64.b64encode(data).decode(), width=w, height=h))
if not kept:
    raise SystemExit("render_size")
sys.stdout.write(json.dumps(dict(pages=pages, landscape=landscape, images=kept)))
"""


class RenderUnavailable(Exception):
    """A rendering that cannot be produced; the text preview stays."""


@dataclass(frozen=True)
class Page:
    jpeg: bytes
    width: int
    height: int


@dataclass(frozen=True)
class Rendered:
    pages: tuple[Page, ...]
    total: int
    landscape: bool


def render_id(deliverable_id: str) -> str:
    """The derived resource of one deliverable's gallery, stable across retries."""
    return str(uuid5(UUID(deliverable_id), RENDER_VERSION))


def tools() -> dict[str, str] | None:
    """Absolute paths of LibreOffice and poppler, or None when one is missing."""
    found = {
        name: shutil.which(name, path=os.environ.get("PATH", "/usr/bin:/bin"))
        for name in ("soffice", "pdftoppm", "pdfinfo")
    }
    if any(path is None for path in found.values()):
        return None
    return {name: str(path) for name, path in found.items()}


_isolation: list[str] | None = None
_isolation_checked = False


def isolation() -> list[str] | None:
    """The command prefix that removes network access from the renderer.

    As root a new network namespace needs no user namespace; otherwise an
    unprivileged user namespace maps the caller. None when neither works: the
    renderer then never runs (LibreOffice may fetch linked content).
    """
    global _isolation, _isolation_checked
    if _isolation_checked:
        return _isolation
    unshare = shutil.which("unshare")
    if unshare is not None:
        for prefix in ([unshare, "--net"], [unshare, "--net", "--map-root-user"]):
            try:
                probe = subprocess.run(
                    [*prefix, "true"], capture_output=True, timeout=5, env={}
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                _isolation = prefix
                break
    _isolation_checked = True
    return _isolation


_slots = threading.BoundedSemaphore(1)


def render_pages(content: bytes, suffix: str, *, timeout: float) -> Rendered:
    """Convert and rasterise in the bounded child; raise RenderUnavailable."""
    if suffix not in RENDER_SUFFIXES:
        raise RenderUnavailable("format")
    if not 0 < len(content) <= 16 * 1024 * 1024:
        raise RenderUnavailable("artifact_size")
    found = tools()
    if found is None:
        raise RenderUnavailable("renderer_absent")
    prefix = isolation()
    if prefix is None:
        raise RenderUnavailable("no_network_isolation")
    deadline = time.monotonic() + timeout
    # One rendering at a time per process: LibreOffice takes about 200 MB.
    if not _slots.acquire(timeout=max(0.0, timeout - 1)):
        raise RenderUnavailable("busy")
    work: Path | None = None
    try:
        work = Path(tempfile.mkdtemp(prefix="atlas-render-"))
        config = dict(
            work=str(work),
            suffix=suffix,
            max_pages=MAX_PAGES,
            # The page around the pictures takes a few kilobytes.
            budget=RENDER_LIMIT - 16384,
            **found,
        )
        try:
            process = subprocess.Popen(
                [*prefix, sys.executable, "-c", RENDER_PROGRAM, json.dumps(config)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Path(__file__).resolve().parents[2],
                env=dict(
                    PATH="/usr/bin:/bin",
                    HOME=str(work),
                    TMPDIR=str(work),
                    LANG="C.UTF-8",
                    SAL_USE_VCLPLUGIN="svp",
                ),
                start_new_session=True,
            )
        except OSError:
            raise RenderUnavailable("spawn") from None
        try:
            stdout, stderr = process.communicate(
                content, timeout=max(1.0, deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            kill(process)
            raise RenderUnavailable("timeout") from None
        finally:
            # LibreOffice runs as a grandchild; never leave one behind.
            kill(process)
        if process.returncode != 0:
            lines = stderr.decode("utf-8", "replace").strip().splitlines()
            raise RenderUnavailable((lines[-1] if lines else "exit")[:80])
        try:
            value = json.loads(stdout)
            pages = tuple(
                Page(
                    base64.b64decode(item["jpeg"], validate=True),
                    int(item["width"]),
                    int(item["height"]),
                )
                for item in value["images"]
            )
            total = int(value["pages"])
            landscape = bool(value["landscape"])
        except (ValueError, KeyError, TypeError):
            raise RenderUnavailable("malformed_output") from None
        if not pages or len(pages) > MAX_PAGES or total < len(pages):
            raise RenderUnavailable("malformed_output")
        if any(not page.jpeg.startswith(b"\xff\xd8\xff") for page in pages):
            raise RenderUnavailable("malformed_output")
        return Rendered(pages, total, landscape)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        _slots.release()


def kill(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        # Collects what is left and closes the pipes.
        process.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


STYLE = """
:root{color-scheme:light dark;--bg:#f3f3f1;--fg:#1c1c1a;--muted:#5f5f5a;--line:#d6d6d0;--chip:#e6e6e1;--accent:#1f5fbf}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#a6a6a0;--line:#3a3a38;--chip:#2a2a28;--accent:#8ab4f8}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:1;display:flex;gap:12px;align-items:center;justify-content:space-between;padding:10px 16px;background:var(--bg);border-bottom:1px solid var(--line)}
h1{margin:0;font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.meta{display:flex;gap:10px;align-items:center;flex:none;color:var(--muted);font-variant-numeric:tabular-nums}
.meta span,.get{display:inline-flex;gap:6px;align-items:center}
svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
a{color:var(--accent);text-decoration:none}
.get{padding:5px 10px;border-radius:999px;background:var(--chip);font-size:13px;font-weight:600}
a:hover,a:focus-visible{text-decoration:underline}
img{display:block;width:100%;height:auto;background:#fff;border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.18),0 0 0 1px var(--line)}
figure{margin:0}
figcaption{display:flex;gap:8px;justify-content:center;align-items:center;padding:8px 0 0;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.nav{display:inline-flex;align-items:center;justify-content:center;width:34px;height:30px;border-radius:999px;background:var(--chip);font-size:18px;line-height:1}
.nav.off{visibility:hidden}
.pages{max-width:920px;margin:0 auto;padding:16px;display:grid;gap:20px}
.deck{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;scroll-behavior:smooth;gap:16px;padding:16px;overscroll-behavior-x:contain}
.deck figure{flex:0 0 100%;scroll-snap-align:center;scroll-snap-stop:always;max-width:1100px}
.deck img{width:auto;max-width:100%;max-height:calc(100vh - 120px);margin:0 auto}
@media (min-width:1200px){.deck figure{flex-basis:1100px}}
@media (orientation:portrait) and (max-width:900px){.deck{flex-direction:column;overflow:visible;scroll-snap-type:none;gap:20px}.deck figure{flex:none}.nav{display:none}}
.more{text-align:center;color:var(--muted);padding:0 16px 24px}
@media (prefers-reduced-motion:reduce){.deck{scroll-behavior:auto}}
"""
# Language-neutral symbols: pages, slides, download.
ICON_PAGES = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/></svg>'
ICON_SLIDES = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="12" rx="1"/><path d="M12 17v3M8 21h8"/></svg>'
ICON_DOWNLOAD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>'


def gallery_html(rendered: Rendered, name: str, download_route: str | None) -> bytes:
    """A static page of the pictures; labels are symbols and numbers only.

    The gallery is stored once per deliverable whatever the reader's interface
    language, so it holds no words: the file name, "3 / 12" counters, the
    page count and a download chip named by the file's format.
    """
    count = len(rendered.pages)
    title = html.escape(name or "preview", quote=True)
    extension = html.escape(Path(name).suffix.lstrip(".").upper()[:8], quote=True)
    route = html.escape(download_route, quote=True) if download_route else ""
    icon = ICON_SLIDES if rendered.landscape else ICON_PAGES
    parts = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>{title}</title><style>{STYLE}</style></head><body>",
        f'<header><h1 title="{title}">{title}</h1><div class="meta">',
        f'<span aria-label="{rendered.total}">{icon}{rendered.total}</span>',
    ]
    if route:
        parts.append(
            f'<a class="get" href="{route}" download aria-label="{title}">'
            f"{ICON_DOWNLOAD}{extension}</a>"
        )
    parts.append("</div></header>")
    parts.append(f'<main class="{"deck" if rendered.landscape else "pages"}">')
    for index, page in enumerate(rendered.pages, 1):
        data = base64.b64encode(page.jpeg).decode()
        caption = f"<span>{index} / {rendered.total}</span>"
        if rendered.landscape:
            back = (
                f'<a class="nav" href="#p{index - 1}" aria-label="{index - 1}">‹</a>'
                if index > 1
                else '<span class="nav off"></span>'
            )
            ahead = (
                f'<a class="nav" href="#p{index + 1}" aria-label="{index + 1}">›</a>'
                if index < count
                else '<span class="nav off"></span>'
            )
            caption = back + caption + ahead
        parts.append(
            f'<figure id="p{index}"><img src="data:image/jpeg;base64,{data}" '
            f'width="{page.width}" height="{page.height}" '
            f'alt="{index} / {rendered.total}">'
            f"<figcaption>{caption}</figcaption></figure>"
        )
    parts.append("</main>")
    if count < rendered.total:
        more = f"1–{count} / {rendered.total}"
        if route:
            more = f'<a class="get" href="{route}" download>{ICON_DOWNLOAD}{more}</a>'
        parts.append(f'<p class="more">{more}</p>')
    parts.append("</body></html>")
    return "".join(parts).encode("utf-8")


def render_preview(
    content: bytes,
    suffix: str,
    name: str,
    download_route: str | None,
    *,
    timeout: float,
) -> bytes | None:
    """The gallery page of verified artifact bytes, or None (reason logged)."""
    started = time.monotonic()
    try:
        rendered = render_pages(content, suffix, timeout=timeout)
        page = gallery_html(rendered, name, download_route)
    except RenderUnavailable as error:
        logging.getLogger(__name__).warning(
            "deliverable_render_unavailable suffix=%s reason=%s", suffix, error
        )
        return None
    if len(page) > RENDER_LIMIT:
        logging.getLogger(__name__).warning(
            "deliverable_render_unavailable suffix=%s reason=render_size", suffix
        )
        return None
    logging.getLogger(__name__).info(
        "deliverable_rendered suffix=%s pages=%d shown=%d bytes=%d seconds=%.1f",
        suffix,
        rendered.total,
        len(rendered.pages),
        len(page),
        time.monotonic() - started,
    )
    return page


_failures: dict[str, float] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        if len(_locks) > 256:
            for old in [k for k, lock in _locks.items() if not lock.locked()]:
                del _locks[old]
        return _locks.setdefault(key, threading.Lock())


def recently_failed(deliverable_id: str) -> bool:
    failed = _failures.get(deliverable_id)
    return failed is not None and time.monotonic() - failed < FAILURE_SECONDS


def gallery_headers() -> dict[str, str]:
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": GALLERY_CSP,
        "Content-Disposition": "inline",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def ensure_rendered(
    repository: Any, identity: Any, deliverable_id: str, *, timeout: float
) -> bytes | None:
    """The gallery of a deliverable, rendered and stored once; None to keep text.

    The artifact bytes are read in one short transaction and the gallery is
    stored in another: the conversion never holds the project connection. A
    reader who may not write (a viewer) still gets the page rendered for them;
    the publisher's background rendering is the one normally stored.
    """
    import psycopg

    from services.retrieval.project_files import ProjectFiles
    from services.retrieval.project_identity import ProjectError

    log = logging.getLogger(__name__)
    if recently_failed(deliverable_id):
        return None
    deadline = time.monotonic() + timeout
    lock = _lock(deliverable_id)
    if not lock.acquire(timeout=max(0.0, timeout - 1)):
        return None
    try:
        try:
            with repository.scope(identity, identity.org, identity.project) as scope:
                files = ProjectFiles(scope)
                value = files.deliverable(deliverable_id)
                existing = files.rendered_preview(value)
                if existing is not None:
                    return existing
                source = files.deliverable_source(value)
        except (ProjectError, psycopg.Error, OSError, ValueError) as error:
            # Access is decided per reader: never remembered as a failure.
            log.warning("deliverable_render_unreadable reason=%s", type(error).__name__)
            return None
        if source is None:
            _failures[deliverable_id] = time.monotonic()
            return None
        content, suffix, name = source
        page = render_preview(
            content,
            suffix,
            name,
            value.get("download_route"),
            timeout=max(1.0, deadline - time.monotonic()),
        )
        if page is None:
            _failures[deliverable_id] = time.monotonic()
            return None
        try:
            with repository.scope(identity, identity.org, identity.project) as scope:
                files = ProjectFiles(scope)
                value = files.deliverable(deliverable_id)
                if files.rendered_preview(value) is None:
                    files.store_rendered_preview(value, page)
        except (ProjectError, psycopg.Error, OSError, ValueError) as error:
            log.warning("deliverable_render_not_stored reason=%s", type(error).__name__)
        return page
    finally:
        lock.release()
        if len(_failures) > 1024:
            for key in sorted(_failures, key=_failures.__getitem__)[:512]:
                _failures.pop(key, None)
