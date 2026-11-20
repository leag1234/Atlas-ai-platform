# Media named by the person, embedded in the app

Status: implemented (app turns only). Code: `services/orchestrator/app_media.py`
(download), `packages/artifact_media.py` (references, embedding, follow-ups).

## Problem

The side view blocks every network access (`connect-src 'none'`, `img-src` and
`media-src` limited to `data:` and `blob:`). Staging, 2026-11-20: a drum machine was
asked with the addresses of real samples; the first file loaded them from the web,
the static check rejected it, and the repaired app had synthesized sounds only, while
the answer still described "real samples" (docs/26-app-edits-in-place.md, open points).

## Decision (supervisor, 2026-11-20, under the owner's self-check rule)

When the request of an app turn gives https addresses of audio or image files, Atlas
downloads them on the server and embeds them in the stored file, so the sandbox stays
closed. The answer says which files are embedded, with their licence or "licence
unknown", and which are not, with the reason.

## How

1. Addresses: https URLs in the last request whose path ends in an audio or image
   extension (WAV, MP3, OGG, Opus, FLAC, M4A, AAC, PNG, JPEG, GIF, WebP). Other links
   (pages given as examples) are left alone; an `http://` media address is reported
   as refused.
2. Download (SSRF-safe, the boundary of `services/orchestrator/web.py`): https only,
   also after each redirect (at most 3, each checked again); the host and every
   resolved address must be public (no loopback, private, link-local, reserved,
   IPv4-mapped or 6to4/Teredo address; checked at the connector, so a DNS answer
   cannot rebind); no credentials or non-standard ports; identity encoding only.
   Limits: 1.5 MB per file, 4 MB per turn (`ATLAS_APP_MEDIA_MAX_FILE_BYTES`,
   `ATLAS_APP_MEDIA_MAX_TOTAL_BYTES`), 12 files, 15 s per file, at most 30 s for the
   downloads of a turn. Not in strict privacy (nothing leaves Atlas there).
3. Type: decided by the bytes (file signature), whatever the server declares; SVG is
   refused (it can carry script). An HTML error page served as `kick.wav` is refused.
4. Licence: the one the person names next to the address (or the only one named in
   the request), else the one the source site states (an HTTP `Link: rel="license"`
   header; for Wikimedia Commons files, `LicenseShortName` from the Commons API),
   else unknown, and the answer says to check the rights before sharing the app.
5. The model never sees the bytes. It is told the references (`atlas-media:kick`,
   type, size, file name) and writes them as URLs (`<img src>`, `<audio src>`, CSS
   `url()`) or, for Web Audio, `ctx.decodeAudioData(atlasMedia.buffer("atlas-media:kick"))`
   (a fetch of a `data:` URI is a connection, which the policy blocks).
6. The static checks read the code with each reference replaced by a short `data:`
   stand-in, so the 256 KB code limit and the script checks apply to the code, not to
   the samples (base64 can contain `+//`, which the script check would read as an
   address). An unknown reference is a problem the repair call fixes. Once the file
   passes, the bytes are embedded as `data:` URIs, with a small decoder
   (`atlasMedia.buffer`) and a JSON manifest (name, source address, licence) at the
   top of `<head>`. The render check (docs/27) then opens that final file.
7. Follow-ups: the stored file is read back with references instead of bytes
   (`artifact_media.abstract`, from the manifest; any other inline `data:` URI of
   2 KB or more becomes a reference too), so an app with samples stays editable (the
   editable limit of 96 KB applies to the code, files up to 8 MB are read) and the
   next version embeds the same bytes again.

Files the person attaches to the conversation are not covered here.
