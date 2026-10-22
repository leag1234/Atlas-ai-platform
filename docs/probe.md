# Daily live acceptance probe (M26)

Run `scripts/daily-probe.sh`. It loads the same external credentials as
`make serve`, starts a temporary CPU gateway, and exercises the public HTTP
application through the live acceptance runner. It reuses an available terminal
or starts and cleans up its own. The application process selects SerpAPI,
independently of the interactive stack’s provider. Each request uses
the first public profile, the ordinary or attachment budget, and real providers.
The six cases check downloaded presentation, corrected document and spreadsheet
content, the owner's structured PDF summary, a changing web fact and plain text.
The owner fixture is read only from `/opt/atlas-src/private/fixtures/owner-174p.pdf`.
It and all captures remain outside tracked files. No private evaluation is read.

Installed on this host on 2026-10-13 and read back from crontab. The weekday morning entry (UTC) is:

```cron
0 7 * * 1-5 cd /opt/atlas-src/repo && scripts/daily-probe.sh >> BRAIN/daily-probe.log 2>&1
```

`BRAIN/probe/YYYY-MM-DD.json` records status, elapsed seconds, cost, HTTP status
and error code/message for each case. A provider quota refusal records
`quota_exceeded` and its provider HTTP status (for example432) in the message. Any failure writes `BRAIN/PROBE_FAILED.txt` with
the failed case and measured values; a fully successful run removes that marker.
A provider or terminal failure is a failed probe, never a passing replay. The lock prevents
overlapping scheduled executions. Local milestone acceptance uses `make verify-m26`;
CI regression replays never certify live acceptance.

Owner ruling (2026-10-13): the probe uses SerpAPI; J59 and live acceptance
use Tavily. The launcher records free account-balance checks before and after
the probe in `BRAIN/search-allowance.jsonl`. It reserves 50 SerpAPI searches
plus the maximum 18 searches for the six requests before starting. Tavily's
150-credit reserve is preserved during acceptance and corpus batches. Search
authorization lasts 30 days from October 13; review scheduling at expiry.
SerpAPI HTTP429 reports exhausted quota or the hourly limit distinctly.
