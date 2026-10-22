# SearXNG sidecar

ATLAS starts this self-hosted search provider only when
`ATLAS_SEARCH_PROVIDER=searxng`. It is bound to loopback and is removed when
`make serve` stops. The image is pinned to
`searxng/searxng:2026.10.17-1c10abe2d@sha256:8bdd9aa136b2dc3b9332cf24714fa34a375b8a1949a1201ec900c24d7378596e`.

Source: Docker Hub `searxng/searxng` tag metadata, retrieved 2026-10-18. The
tag resolved to this manifest digest; the upstream project is
<https://github.com/searxng/searxng> (AGPL-3.0-or-later).

## Owner operation

Generate a runtime-only secret before starting the stack. Do not put it in a
file committed to Git:

```bash
export SEARXNG_SECRET="$(openssl rand -hex 32)"
export ATLAS_SEARCH_PROVIDER=searxng
export ATLAS_PORT_BASE=1800
make serve
```

The launcher maps the configured SearXNG port to `127.0.0.1` only. It mounts
this settings file read-only and uses an offset-qualified Docker volume named
`atlas-searxng-data-<base>` for runtime data. The settings enable only HTML and
JSON responses, moderate safe search, automatic language detection, no limiter,
no image proxy, no metrics, and no autocomplete. Granian access logging is
disabled so query URLs are not written to container logs.

To use a different stack offset, set `ATLAS_PORT_BASE`; the provider port is
the `searxng` entry in `packages/ports.py` (8888 plus the offset). Do not expose
that port through a public firewall rule.
