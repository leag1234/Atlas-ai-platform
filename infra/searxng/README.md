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
disabled so query URLs are not written to container logs. Every engine is
disabled for category searches: ATLAS names one engine per request, and a
request that names no loaded engine reaches no engine instead of all of them
(see `runbooks/searxng.md`). Changing this file takes effect when the sidecar
container is recreated, which `make serve` does at each start.

To use a different stack offset, set `ATLAS_PORT_BASE`; the provider port is
the `searxng` entry in `packages/ports.py` (8888 plus the offset). Do not expose
that port through a public firewall rule.

## Several outgoing addresses

Public engines rate limit and block by client address. When every sidecar of a
host leaves through the host's one public address, a burst from one stack (a
test battery, a prototype) gets the engines to refuse the others too: on
2026-11-19 about 20 searches in 10 minutes from staging tests suspended six
engines for the staging sidecar, from the address production also uses.

The sidecar settings stay the same on every host. The host gives each sidecar
a pool of public addresses by source NAT, so the containers keep their bridge
network and no SearXNG option depends on the host:

- production's sidecar leaves through the primary address plus additional
  addresses; every other sidecar (staging, worktree stacks, tests) through a
  disjoint pool, so tests never spend production's addresses;
- each new TCP connection from a sidecar takes the next address of its pool
  (an `iptables` nat chain jumped first from `POSTROUTING`, one `statistic
  --mode nth` rule per address, matched on the container's address);
- host processes keep the primary address, and DNS keeps Docker's masquerade.

A host script keeps the chain in step with the running sidecars every 20
seconds (a container gets a new bridge address when it is recreated) and adds
a missing pool address to the interface. On the reference VM it is
`/opt/atlas-src/tools/search_egress.sh` with its pools in `search_egress.conf`
next to it, run by `atlas-search-egress.timer`; `search_egress.sh status`
shows the pools and per-address counters, `search_egress.sh off` removes the
chain. Pool addresses are host configuration and are not committed.

SearXNG's own `outgoing.source_ips` (and `outgoing.proxies`) rotate addresses
too, but only addresses the container owns: they need host networking, which
gives the sidecar the host's loopback services, or one proxy per address. See
<https://docs.searxng.org/admin/settings/settings_outgoing.html>.

### Choosing addresses

SearXNG suspends an engine for every address of the sidecar when one address
is refused (the suspension belongs to the engine, not to the address). One
address an engine distrusts therefore costs the whole pool that engine. Before
adding an address to a pool, ask each engine once from it, at least 8 seconds
apart, and stop at the first refusal; keep only addresses that pass for the
engines of the default order.

Measured on 2026-11-19 from a Scaleway instance (fr-par-2), one query per
engine and address, at least 9 seconds apart, 40 queries in all: the primary
IPv4 address (blocked by DuckDuckGo since the morning burst), five fresh IPv4
addresses (A to E) and three IPv6 addresses in two /64 prefixes:

| Engine | IPv4 | IPv6 |
| --- | --- | --- |
| Startpage | answers (primary, A, B, C) | no IPv6 endpoint |
| DuckDuckGo | answers (B, C, D, E); CAPTCHA then access denied (A); timeout (primary) | no IPv6 endpoint |
| Mojeek | answers (primary, A, B, C) | no IPv6 endpoint |
| Seznam | answers (A, B, C) | answers (both prefixes, two addresses in one) |
| Bing | answers (A, B, C) | answers (both prefixes, two addresses in one) |
| Qwant | access denied (A, B, C) | no IPv6 endpoint |
| Brave | too many requests (A, B, C) | too many requests (both prefixes) |
| Google | access denied (A, B, C) | access denied (both prefixes) |

Address A was released and replaced by D and E. In a burst of 20 questions
over 12 minutes on staging afterwards (44 searches, pool D and E), Startpage
answered 42 of 42 calls, Seznam 1 of 1 and Mojeek refused its only call
(access denied); on the same morning burst from the primary address six
engines had been suspended.

The engines that carry the default order (Startpage, DuckDuckGo, Mojeek) have
no IPv6 endpoint, and Google and Brave refuse this provider's IPv6 prefixes as
they refuse its IPv4 addresses, so the pools are IPv4. IPv6 would only add a
separate identity for Seznam and Bing; it needs IPv6 on the sidecar's Docker
network and was not deployed. Many engines treat a whole /64 as one client, so
extra addresses inside one prefix are not extra identities.

### Adding an address on Scaleway

Instances with routed IPs hold up to five flexible IPv4 addresses. Reserve and
attach one with the Instance API (`POST /instance/v1/zones/<zone>/ips` with
`type: routed_ipv4` and the server id). The Scaleway agent then renders it into
the netplan configuration and reconfigures the interface itself; each attach
or release makes `systemd-networkd` renew the DHCP lease of the primary
address, which is brief, so attach several addresses in one go. Measure the
address as above, add it to a pool in the host configuration, and run the
script. A flexible IPv4 costs 0.005 EUR per hour (about 3.65 EUR per month,
public catalogue 2026-11-19); IPv6 prefixes are free.
