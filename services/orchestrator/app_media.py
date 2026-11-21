"""Audio and image files the person names for an app, fetched by Atlas.

The preview blocks every network access, so an app cannot load the samples or
pictures the person points to. When the request of an app turn gives https
addresses of audio or image files, Atlas downloads them on the server and the
app embeds them (packages/artifact_media.py):

- https only, public addresses only: the address and every resolved IP are
  checked at the connector (services/orchestrator/web.py: no loopback,
  private, link-local or reserved address), redirects are followed by hand
  and checked again (at most MAX_REDIRECTS);
- the content must be one of the allowed audio or image types, by its own
  bytes (signature), whatever the server declares;
- an SVG file is a document (it can carry script, event handlers and
  references to other addresses), never embedded as such: it is drawn as a
  PNG in an isolated browser without network (svg_raster.py) and only the
  pixels enter the app; one that cannot be drawn safely is named in the
  answer with the reason;
- a wiki file page (``https://commons.wikimedia.org/wiki/File:Flag.svg``)
  is read as the file itself (``Special:FilePath``);
- MAX_FILE_BYTES per file, MAX_TOTAL_BYTES for the turn, MAX_FILES files,
  FILE_SECONDS per file within the turn's deadline;
- not in strict privacy (no request leaves Atlas there).

Licence: the one the person states in the request (on the same line as the
address, else a single licence stated for the whole request), else the one the
hosting site states (an HTTP ``Link: rel="license"`` header; for Wikimedia
Commons files, the file page's licence from the Commons API), else unknown,
and the answer says so.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp

from packages.artifact_media import Media, slug
from packages.limits import LimitError
from services.orchestrator import svg_raster
from services.orchestrator.web import (
    USER_AGENT,
    PublicResolver,
    http_timeout,
    validate_url,
)

log = logging.getLogger(__name__)

MAX_FILES = 12
MAX_FILE_BYTES = 1_500_000
MAX_TOTAL_BYTES = 4_000_000
FILE_SECONDS = 15.0
LICENCE_SECONDS = 5.0
MAX_REDIRECTS = 3

AUDIO_EXTENSIONS = (".wav", ".mp3", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
EXTENSIONS = AUDIO_EXTENSIONS + IMAGE_EXTENSIONS
_URL = re.compile(r"\b(?:https?)://[^\s<>\"'`)\]]+", re.IGNORECASE)
_TRAILING = ".,;:!?»”’"
# Wikimedia projects: a file page (any namespace name: File, Fichier,
# Datei...) is served as the file by Special:FilePath.
_WIKI_HOST = re.compile(
    r"(?:^|\.)(?:wikipedia|wikimedia|wikibooks|wikinews|wikiquote|wikisource|"
    r"wikiversity|wikivoyage|wiktionary|wikidata)\.org$"
)
_WIKI_FILE = re.compile(r"^/wiki/[^/:]+:([^/]+)$")

# Licences a person or a page names, most specific first.
_LICENCES = (
    (re.compile(r"\bCC0\b|\bCC[- ]?zero\b", re.IGNORECASE), "CC0"),
    (
        re.compile(
            r"\bCC[- ]BY(?:[- ](?:SA|NC|ND)){0,3}(?:[- ]\d\.\d)?\b", re.IGNORECASE
        ),
        "",
    ),
    (
        re.compile(
            r"\bpublic domain\b|\bdomaine public\b|\bgemeinfrei\b|"
            r"\bdominio p[uú]blico\b|\bpubblico dominio\b",
            re.IGNORECASE,
        ),
        "public domain",
    ),
    (re.compile(r"\bMIT licen[cs]e\b|\blicen[cs]e MIT\b", re.IGNORECASE), "MIT"),
)


@dataclass(frozen=True)
class MediaFailure:
    url: str
    # not_https, invalid_url, private_address, wrong_type, too_large,
    # total_too_large, too_many, timeout, http_<status>, redirect_limit,
    # strict_privacy, unavailable, unused (downloaded, not used by the app),
    # svg_invalid, svg_too_complex, svg_blank, svg_render_failed,
    # svg_no_renderer, svg_timeout (svg_raster.py)
    reason: str


def max_file_bytes() -> int:
    return _int("ATLAS_APP_MEDIA_MAX_FILE_BYTES", MAX_FILE_BYTES)


def max_total_bytes() -> int:
    return _int("ATLAS_APP_MEDIA_MAX_TOTAL_BYTES", MAX_TOTAL_BYTES)


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def media_urls(text: str) -> list[str]:
    """Addresses of audio or image files in the request, in order, once each."""
    found: list[str] = []
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(_TRAILING)
        path = unquote(urlsplit(url).path).lower()
        if path.endswith(EXTENSIONS) and url not in found:
            found.append(url)
    return found


def direct_url(url: str) -> str:
    """The address of the file itself: a wiki file page becomes its file."""
    parts = urlsplit(url)
    found = _WIKI_FILE.match(parts.path)
    if (
        parts.scheme == "https"
        and found is not None
        and _WIKI_HOST.search((parts.hostname or "").lower())
        and unquote(found.group(1)).lower().endswith(EXTENSIONS)
    ):
        return f"https://{parts.netloc}/wiki/Special:FilePath/{found.group(1)}"
    return url


def _normal(found: str, label: str) -> str:
    if label:
        return label
    parts = re.split(r"[- ]+", found.upper())
    version = parts[-1] if re.fullmatch(r"\d\.\d", parts[-1]) else ""
    terms = [part for part in parts[1:] if part != version]
    return "CC " + "-".join(terms) + (" " + version if version else "")


def _licences(text: str) -> list[str]:
    """The licences named in ``text``, in order of appearance, once each."""
    found: list[tuple[int, str]] = []
    for pattern, label in _LICENCES:
        for match in pattern.finditer(text):
            found.append((match.start(), _normal(match.group(0), label)))
    names: list[str] = []
    for _, name in sorted(found):
        if name not in names:
            names.append(name)
    return names


def stated_licence(text: str, url: str) -> str:
    """The licence the person gives for ``url``.

    The one named after the address (up to the next address on its line), else
    before it on its line, else the only licence named in the whole request.
    """
    for line in text.splitlines():
        position = line.find(url)
        if position < 0:
            continue
        others = [match.span() for match in _URL.finditer(line)]
        after_end = min(
            (start for start, _ in others if start >= position + len(url)),
            default=len(line),
        )
        before_start = max((end for _, end in others if end <= position), default=0)
        for segment in (
            line[position + len(url) : after_end],
            line[before_start:position],
        ):
            local = _licences(segment)
            if len(local) == 1:
                return local[0]
    every = _licences(" ".join(_URL.sub(" ", text).split()))
    return every[0] if len(every) == 1 else ""


_SIGNATURES = (
    (b"RIFF", 8, b"WAVE", "audio/wav"),
    (b"RIFF", 8, b"WEBP", "image/webp"),
    (b"OggS", 0, b"", "audio/ogg"),
    (b"fLaC", 0, b"", "audio/flac"),
    (b"ID3", 0, b"", "audio/mpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, b"", "image/png"),
    (b"\xff\xd8\xff", 0, b"", "image/jpeg"),
    (b"GIF87a", 0, b"", "image/gif"),
    (b"GIF89a", 0, b"", "image/gif"),
)


def sniff(data: bytes) -> str:
    """The media type of the bytes by their signature, or "" if not allowed."""
    for prefix, offset, mark, mime in _SIGNATURES:
        if data.startswith(prefix) and (not mark or data[offset : offset + 4] == mark):
            return mime
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        # MPEG audio frame (MP3 without an ID3 tag) or ADTS AAC.
        return "audio/aac" if data[1] & 0x06 == 0 else "audio/mpeg"
    if data[4:8] == b"ftyp" and data[8:11] in {b"M4A", b"mp4", b"iso", b"M4B"}:
        return "audio/mp4"
    return ""


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])[:80]


class Fetcher:
    """One turn's downloads, sharing the size budget and the deadline."""

    def __init__(self, deadline_seconds: float) -> None:
        self.loop = asyncio.get_running_loop()
        self.deadline = self.loop.time() + deadline_seconds
        self.total = 0

    def left(self) -> float:
        return self.deadline - self.loop.time()

    async def _get(
        self, session: aiohttp.ClientSession, url: str, limit: int, timeout: float
    ) -> tuple[str, bytes, str]:
        """The final address, the bytes (at most ``limit``) and the licence header."""
        for _ in range(MAX_REDIRECTS + 1):
            if urlsplit(url).scheme != "https":
                raise ValueError("not_https")
            validate_url(url)
            async with session.get(
                url, allow_redirects=False, timeout=http_timeout(timeout, timeout)
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status != 200:
                    raise ValueError(f"http_{response.status}")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("wrong_type")
                declared = response.content_length
                if declared is not None and declared > limit:
                    raise LimitError("too_large", declared, limit, "bytes")
                body = bytearray()
                async for piece in response.content.iter_chunked(65536):
                    body.extend(piece)
                    if len(body) > limit:
                        raise LimitError("too_large", len(body), limit, "bytes")
                return url, bytes(body), _link_licence(response.headers)
        raise ValueError("redirect_limit")

    async def fetch(
        self, session: aiohttp.ClientSession, url: str, text: str, taken: set[str]
    ) -> Media | MediaFailure:
        timeout = min(FILE_SECONDS, self.left())
        if timeout < 1:
            return MediaFailure(url, "timeout")
        room = max_total_bytes() - self.total
        limit = min(max_file_bytes(), room)
        if limit <= 0:
            return MediaFailure(url, "total_too_large")
        target = direct_url(url)
        try:
            async with asyncio.timeout(timeout):
                final, data, header_licence = await self._get(
                    session, target, limit, timeout
                )
        except TimeoutError:
            return MediaFailure(url, "timeout")
        except LimitError:
            code = "too_large" if limit == max_file_bytes() else "total_too_large"
            return MediaFailure(url, code)
        except ValueError as error:
            return MediaFailure(url, _reason(str(error)))
        except aiohttp.ClientError as error:
            if "ssrf_denied" in repr(error) or "dns_empty" in repr(error):
                return MediaFailure(url, "private_address")
            return MediaFailure(url, "unavailable")
        except OSError:
            return MediaFailure(url, "unavailable")
        mime = sniff(data) or (svg_raster.MIME if svg_raster.is_svg(data) else "")
        if not mime:
            return MediaFailure(url, "wrong_type")
        self.total += len(data)
        licence, source = stated_licence(text, url), "person"
        if not licence:
            licence, source = header_licence, "page"
        if not licence and urlsplit(final).hostname == "upload.wikimedia.org":
            licence = await self._commons_licence(session, final)
        if not licence:
            source = ""
        filename = _filename(target)
        return Media(slug(filename, taken), mime, data, url, filename, licence, source)

    async def _commons_licence(self, session: aiohttp.ClientSession, url: str) -> str:
        """The licence a Wikimedia Commons file page states, or ""."""
        name = _filename(url)
        if not name or self.left() < 1:
            return ""
        api = (
            "https://commons.wikimedia.org/w/api.php?action=query&format=json"
            "&prop=imageinfo&iiprop=extmetadata&iiextmetadatafilter=LicenseShortName"
            "&titles=" + quote("File:" + name)
        )
        try:
            async with asyncio.timeout(min(LICENCE_SECONDS, self.left())):
                _, body, _ = await self._get(session, api, 200_000, LICENCE_SECONDS)
            pages = json.loads(body).get("query", {}).get("pages", {})
            for page in pages.values():
                for info in page.get("imageinfo", []):
                    value = info["extmetadata"]["LicenseShortName"]["value"]
                    return re.sub(r"<[^>]*>", "", str(value)).strip()[:60]
        except (
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            LimitError,
            aiohttp.ClientError,
            OSError,
        ):
            return ""
        return ""


def _reason(code: str) -> str:
    if code in {"ssrf_denied", "dns_empty"}:
        return "private_address"
    if code.startswith("http_") or code in {
        "not_https",
        "redirect_limit",
        "wrong_type",
    }:
        return code
    return "invalid_url" if code == "invalid_url" else "unavailable"


def _link_licence(headers: object) -> str:
    """A licence named by a ``Link: <...>; rel="license"`` header."""
    getall = getattr(headers, "getall", None)
    values = getall("Link", []) if callable(getall) else []
    for value in values:
        for part in str(value).split(","):
            if re.search(r'rel="?license"?', part, re.IGNORECASE):
                target = re.search(r"<([^>]+)>", part)
                if target:
                    found = _licences(target.group(1).replace("/", " "))
                    return found[0] if found else target.group(1)[:120]
    return ""


async def fetch_media(
    text: str,
    seconds: float,
    taken: set[str] | None = None,
    known: set[str] | None = None,
) -> tuple[list[Media], list[MediaFailure]]:
    """The files of the request that could be embedded, and those that could not.

    On a follow-up, ``taken`` holds the reference names the app uses already
    and ``known`` the addresses of the files it embeds (not downloaded again).
    """
    from packages.privacy import PrivacyRefusal, require_shared

    urls = [url for url in media_urls(text) if url not in (known or set())]
    if not urls:
        return [], []
    try:
        require_shared("web_fetch")
    except PrivacyRefusal:
        return [], [MediaFailure(url, "strict_privacy") for url in urls]
    # SVG files are converted after the downloads, in the same turn budget.
    drawings = any(
        unquote(urlsplit(direct_url(url)).path).lower().endswith(".svg") for url in urls
    )
    reserve = min(svg_raster.SECONDS, seconds / 2) if drawings else 0.0
    fetcher = Fetcher(seconds - reserve)
    media: list[Media] = []
    failures: list[MediaFailure] = []
    taken = set(taken or ())
    connector = aiohttp.TCPConnector(
        resolver=PublicResolver(), use_dns_cache=False, force_close=True
    )
    async with aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        auto_decompress=False,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    ) as session:
        for index, url in enumerate(urls):
            if index >= MAX_FILES:
                failures.append(MediaFailure(url, "too_many"))
                continue
            result = await fetcher.fetch(session, url, text, taken)
            if isinstance(result, Media):
                media.append(result)
            else:
                failures.append(result)
    media, failed = await _convert(fetcher, media, max(fetcher.left(), 0.0) + reserve)
    failures += failed
    log.info(
        "app_media fetched=%d failed=%s bytes=%d",
        len(media),
        ",".join(failure.reason for failure in failures) or "-",
        fetcher.total,
    )
    return media, failures


async def _convert(
    fetcher: Fetcher, media: list[Media], seconds: float
) -> tuple[list[Media], list[MediaFailure]]:
    """The downloads with each SVG drawn as a PNG, and the SVGs that are not.

    No SVG leaves this function: each becomes a PNG or a named failure.
    """
    drawings = [item for item in media if item.mime == svg_raster.MIME]
    if not drawings:
        return media, []
    results = dict(
        zip(
            (id(item) for item in drawings),
            await svg_raster.rasterise([item.data for item in drawings], seconds),
            strict=True,
        )
    )
    kept: list[Media] = []
    failures: list[MediaFailure] = []
    for item in media:
        result = results.get(id(item))
        if result is None:
            kept.append(item)
            continue
        fetcher.total -= len(item.data)
        if result.error:
            failures.append(MediaFailure(item.url, result.error))
        elif len(result.png) > max_file_bytes():
            failures.append(MediaFailure(item.url, "too_large"))
        elif fetcher.total + len(result.png) > max_total_bytes():
            failures.append(MediaFailure(item.url, "total_too_large"))
        else:
            fetcher.total += len(result.png)
            kept.append(
                dataclasses.replace(
                    item,
                    mime="image/png",
                    data=result.png,
                    converted_from=svg_raster.MIME,
                    left_out=result.left_out,
                )
            )
    return kept, failures


# The answer says which files are in the app, with their licence, and which
# are not, in the person's language.
NOTE = {
    "fr": {
        "embedded": "Fichiers intégrés à l'application :",
        "failed": "Fichiers non intégrés :",
        "person": "licence {licence} (indiquée par vous)",
        "page": "licence {licence} (indiquée par le site d'origine)",
        "unknown": "licence inconnue : vérifiez les droits avant de partager l'application",
        "not_https": "adresse non https, refusée",
        "invalid_url": "adresse invalide",
        "private_address": "adresse privée ou locale, refusée",
        "wrong_type": "pas un fichier audio ou image accepté (WAV, MP3, OGG, FLAC, M4A, PNG, JPEG, GIF, WebP, SVG)",
        "too_large": "plus de {file_mb} Mo",
        "total_too_large": "les fichiers dépassent {total_mb} Mo au total",
        "too_many": "plus de {files} fichiers",
        "timeout": "téléchargement trop long",
        "http": "le site a répondu {status}",
        "redirect_limit": "trop de redirections",
        "strict_privacy": "rien n'est téléchargé en confidentialité stricte",
        "unavailable": "téléchargement impossible",
        "unused": "téléchargé mais pas utilisé par l'application",
        "converted": "SVG converti en image PNG",
        "left_out": "sans les éléments qu'il chargeait depuis d'autres adresses",
        "svg_invalid": "fichier SVG illisible (pas un document SVG valide)",
        "svg_too_complex": "dessin SVG trop complexe pour être converti (plus de {elements} éléments)",
        "svg_blank": "le SVG ne dessine rien sans les éléments qu'il charge depuis d'autres adresses, refusés par sécurité",
        "svg_render_failed": "le SVG n'a pas pu être converti en image",
        "svg_no_renderer": "les fichiers SVG sont convertis en image dans un navigateur isolé, qui n'est pas disponible sur ce serveur",
        "svg_timeout": "la conversion du SVG en image a pris trop de temps",
    },
    "en": {
        "embedded": "Files embedded in the app:",
        "failed": "Files not embedded:",
        "person": "licence {licence} (as you stated)",
        "page": "licence {licence} (as the source site states)",
        "unknown": "licence unknown: check the rights before sharing the app",
        "not_https": "not an https address, refused",
        "invalid_url": "invalid address",
        "private_address": "private or local address, refused",
        "wrong_type": "not an accepted audio or image file (WAV, MP3, OGG, FLAC, M4A, PNG, JPEG, GIF, WebP, SVG)",
        "too_large": "larger than {file_mb} MB",
        "total_too_large": "the files exceed {total_mb} MB in total",
        "too_many": "more than {files} files",
        "timeout": "the download took too long",
        "http": "the site answered {status}",
        "redirect_limit": "too many redirects",
        "strict_privacy": "nothing is downloaded in strict privacy",
        "unavailable": "could not be downloaded",
        "unused": "downloaded but not used by the app",
        "converted": "SVG converted to a PNG picture",
        "left_out": "without the parts it loaded from other addresses",
        "svg_invalid": "unreadable SVG file (not a valid SVG document)",
        "svg_too_complex": "SVG drawing too complex to convert (more than {elements} elements)",
        "svg_blank": "the SVG draws nothing without the parts it loads from other addresses, which are refused for safety",
        "svg_render_failed": "the SVG could not be converted to a picture",
        "svg_no_renderer": "SVG files are converted to pictures in an isolated browser, which is not available on this server",
        "svg_timeout": "converting the SVG to a picture took too long",
    },
    "de": {
        "embedded": "In die App eingebettete Dateien:",
        "failed": "Nicht eingebettete Dateien:",
        "person": "Lizenz {licence} (von Ihnen angegeben)",
        "page": "Lizenz {licence} (laut Quellseite)",
        "unknown": "Lizenz unbekannt: Prüfen Sie die Rechte, bevor Sie die App teilen",
        "not_https": "keine https-Adresse, abgelehnt",
        "invalid_url": "ungültige Adresse",
        "private_address": "private oder lokale Adresse, abgelehnt",
        "wrong_type": "keine zulässige Audio- oder Bilddatei (WAV, MP3, OGG, FLAC, M4A, PNG, JPEG, GIF, WebP, SVG)",
        "too_large": "größer als {file_mb} MB",
        "total_too_large": "die Dateien überschreiten insgesamt {total_mb} MB",
        "too_many": "mehr als {files} Dateien",
        "timeout": "der Download dauerte zu lange",
        "http": "die Website antwortete {status}",
        "redirect_limit": "zu viele Weiterleitungen",
        "strict_privacy": "im strengen Datenschutz wird nichts heruntergeladen",
        "unavailable": "konnte nicht heruntergeladen werden",
        "unused": "heruntergeladen, aber von der App nicht verwendet",
        "converted": "SVG in ein PNG-Bild umgewandelt",
        "left_out": "ohne die Teile, die es von anderen Adressen lud",
        "svg_invalid": "unlesbare SVG-Datei (kein gültiges SVG-Dokument)",
        "svg_too_complex": "SVG-Zeichnung zu komplex für die Umwandlung (mehr als {elements} Elemente)",
        "svg_blank": "das SVG zeichnet nichts ohne die Teile, die es von anderen Adressen lädt; diese werden aus Sicherheitsgründen abgelehnt",
        "svg_render_failed": "das SVG konnte nicht in ein Bild umgewandelt werden",
        "svg_no_renderer": "SVG-Dateien werden in einem isolierten Browser in Bilder umgewandelt, der auf diesem Server nicht verfügbar ist",
        "svg_timeout": "die Umwandlung des SVG in ein Bild dauerte zu lange",
    },
    "es": {
        "embedded": "Archivos integrados en la aplicación:",
        "failed": "Archivos no integrados:",
        "person": "licencia {licence} (indicada por usted)",
        "page": "licencia {licence} (indicada por el sitio de origen)",
        "unknown": "licencia desconocida: compruebe los derechos antes de compartir la aplicación",
        "not_https": "dirección no https, rechazada",
        "invalid_url": "dirección no válida",
        "private_address": "dirección privada o local, rechazada",
        "wrong_type": "no es un archivo de audio o imagen aceptado (WAV, MP3, OGG, FLAC, M4A, PNG, JPEG, GIF, WebP, SVG)",
        "too_large": "más de {file_mb} MB",
        "total_too_large": "los archivos superan {total_mb} MB en total",
        "too_many": "más de {files} archivos",
        "timeout": "la descarga tardó demasiado",
        "http": "el sitio respondió {status}",
        "redirect_limit": "demasiadas redirecciones",
        "strict_privacy": "en privacidad estricta no se descarga nada",
        "unavailable": "no se pudo descargar",
        "unused": "descargado pero la aplicación no lo usa",
        "converted": "SVG convertido en imagen PNG",
        "left_out": "sin las partes que cargaba desde otras direcciones",
        "svg_invalid": "archivo SVG ilegible (no es un documento SVG válido)",
        "svg_too_complex": "dibujo SVG demasiado complejo para convertirlo (más de {elements} elementos)",
        "svg_blank": "el SVG no dibuja nada sin las partes que carga desde otras direcciones, rechazadas por seguridad",
        "svg_render_failed": "no se pudo convertir el SVG en imagen",
        "svg_no_renderer": "los archivos SVG se convierten en imágenes en un navegador aislado, que no está disponible en este servidor",
        "svg_timeout": "la conversión del SVG en imagen tardó demasiado",
    },
    "it": {
        "embedded": "File incorporati nell'app:",
        "failed": "File non incorporati:",
        "person": "licenza {licence} (indicata da te)",
        "page": "licenza {licence} (indicata dal sito di origine)",
        "unknown": "licenza sconosciuta: verifica i diritti prima di condividere l'app",
        "not_https": "indirizzo non https, rifiutato",
        "invalid_url": "indirizzo non valido",
        "private_address": "indirizzo privato o locale, rifiutato",
        "wrong_type": "non è un file audio o immagine accettato (WAV, MP3, OGG, FLAC, M4A, PNG, JPEG, GIF, WebP, SVG)",
        "too_large": "più di {file_mb} MB",
        "total_too_large": "i file superano {total_mb} MB in totale",
        "too_many": "più di {files} file",
        "timeout": "il download ha richiesto troppo tempo",
        "http": "il sito ha risposto {status}",
        "redirect_limit": "troppi reindirizzamenti",
        "strict_privacy": "nella privacy rigorosa non si scarica nulla",
        "unavailable": "non è stato possibile scaricarlo",
        "unused": "scaricato ma non usato dall'app",
        "converted": "SVG convertito in immagine PNG",
        "left_out": "senza le parti che caricava da altri indirizzi",
        "svg_invalid": "file SVG illeggibile (non è un documento SVG valido)",
        "svg_too_complex": "disegno SVG troppo complesso da convertire (più di {elements} elementi)",
        "svg_blank": "l'SVG non disegna nulla senza le parti che carica da altri indirizzi, rifiutate per sicurezza",
        "svg_render_failed": "non è stato possibile convertire l'SVG in immagine",
        "svg_no_renderer": "i file SVG vengono convertiti in immagini in un browser isolato, non disponibile su questo server",
        "svg_timeout": "la conversione dell'SVG in immagine ha richiesto troppo tempo",
    },
}


def _mb(value: int) -> str:
    return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")


def media_note(language: str, media: list[Media], failures: list[MediaFailure]) -> str:
    """Which files the app embeds (with their licence) and which it does not."""
    if not media and not failures:
        return ""
    labels = NOTE.get(language, NOTE["en"])
    lines: list[str] = []
    if media:
        lines.append(labels["embedded"])
        for item in media:
            if item.licence and item.licence_source in {"person", "page"}:
                licence = labels[item.licence_source].format(licence=item.licence)
            else:
                licence = labels["unknown"]
            origin = item.url
            if item.converted_from:
                origin += ", " + labels["converted"]
                if item.left_out:
                    origin += " " + labels["left_out"]
            lines.append(f"- {item.filename or item.name} ({origin}): {licence}")
    if failures:
        if lines:
            lines.append("")
        lines.append(labels["failed"])
        for failure in failures:
            key = "http" if failure.reason.startswith("http_") else failure.reason
            reason = labels.get(key, labels["unavailable"]).format(
                status=failure.reason.removeprefix("http_"),
                file_mb=_mb(max_file_bytes()),
                total_mb=_mb(max_total_bytes()),
                files=MAX_FILES,
                elements=svg_raster.MAX_ELEMENTS,
            )
            lines.append(f"- {failure.url}: {reason}")
    return "\n".join(lines)
