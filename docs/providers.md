# Inference providers

The status below records the owner's acceptance tests on fresh Ubuntu VMs,
following the installation documentation literally and checking PDF questions,
plain chat, cited web answers and diagrams in a real browser on 2026-10-22/23.
These results establish those tested flows, not every possible capability.

| Provider | Text model | Vision model | Status |
| --- | --- | --- | --- |
| Hetzner | `Qwen/Qwen3.6-35B-A3B-FP8` | `Qwen/Qwen3.6-35B-A3B-FP8` | Verified 2026-10-23 |
| Mistral | `mistral-small-2603` | Not enabled by this preset | Verified 2026-10-23 |
| Scaleway | `qwen3.5-397b-a17b` | `gemma-4-26b-a4b-it` | Verified 2026-10-23 |
| OVHcloud | `Qwen3.5-397B-A17B` | `Qwen2.5-VL-72B-Instruct` | Verified 2026-10-23 |
| IONOS | `Qwen/Qwen3.5-9B` | `Qwen/Qwen3.5-9B` | In progress |

IONOS has not been tested because no account key was available. Hetzner remains
experimental and is not intended for production. Mistral's four acceptance cases
took approximately 15 seconds each; OVHcloud's replacement text model took
18–24 seconds each in the owner's tests. These timings are observations, not SLAs.

OVHcloud's earlier text preset failed plain-chat and diagram acceptance. The new
preset passed all four flows. Both OVHcloud model IDs were additionally confirmed
against the account's live `/v1/models` list on 2026-10-23, and a tiny image request
confirmed that the vision model correctly identified a red square. OVHcloud's
price remains unverified and its context limit remains a conservative Atlas limit.

The OVHcloud catalog also lists `stable-diffusion-xl-base-v10`. Image generation
through that service is a future option; it is not integrated or enabled here.

Use `./atlas setup --reconfigure` to switch an existing installation. Keep `.env`
and the same Compose project so generated secrets and persistent volumes stay
associated. See [Change provider](quickstart.md#change-provider).

Endpoint URLs, model IDs, indicative prices and official source links are maintained
in [providers.yaml](../services/model-gateway/providers.yaml). Setup verifies your
account's model list and a small streaming completion; this connection check is
separate from browser acceptance. Unverified EUR prices disable the monetary
ceiling, while token, time and tool limits remain active. Provider billing still
applies; see the [quickstart pricing notes](quickstart.md#choose-an-inference-provider).

## Refusal classes in logs

Every provider call refused with an HTTP status is logged with the provider's error
identifiers and one class computed by
[provider_refusal.py](../services/model-gateway/provider_refusal.py). The identifiers are
the `type` and `code` fields of the error body, kept only when they are short identifiers
and written with lowercase letters, digits and underscores (60 characters at most). The
provider's message, the request and the key are never written by these lines.

| Class | Status, when the identifiers say nothing | Substrings of `type` or `code` |
| --- | --- | --- |
| `credit` | 402, 423 | `insufficient_quota`, `insufficient_balance`, `insufficient_credit`, `insufficient_fund`, `payment`, `billing`, `credit`, `spending_limit`, `spend_limit` |
| `capacity` | 503 | `capacity`, `overload` |
| `rate_limit` | 429 | `rate_limit`, `ratelimit`, `too_many`, `toomany`, `throttl`, `tokens_per`, `requests_per`, `concurren` |
| `auth` | 401, 403 | `authentication`, `unauthorized`, `unauthorised`, `invalid_api_key`, `invalid_key`, `permission`, `forbidden`, `access_denied` |
| `other` | any other status; timeouts and transport failures | |

HTTP 402 is always `credit`. For any other status the identifiers are read first, in the
order of the table, because an account without credit or a saturated service tier may also
answer with HTTP 429.

Log lines:

- Developer API, one line per refused call:
  `devapi_provider_refusal provider=<provider> model=<model> status=<status> class=<class> type=<type> code=<code>`
  (`-` for a missing identifier). A `credit` refusal also logs `devapi_provider_credit`.
- Developer API, when the refusal triggers the fallback target:
  `devapi_transient_fallback from=… to=… reason=… started=… class=<class> type=<type> code=<code>`.
- Chat transport: the JSON line `provider_stream_failure` carries `refusal_class`, and
  `refusal_type` and `refusal_code` when present.

A `credit` refusal is never reported as an outage. The developer API tries the fallback
target and otherwise answers 402 `provider_quota` with the provider's status. The chat
gateway answers `provider_quota` (HTTP 402 on its JSON route, an error event on its
stream), which the chat reports as "The model provider refused the request: account credit
exhausted (provider status N)."

Sources, checked 2026-11-12:

- Mistral, [error glossary](https://docs.mistral.ai/resources/error-glossary): flat error
  object (`object`, `message`, `type`, `param`, `code`); types `invalid_request_error`,
  `authentication_error`, `rate_limit_error`, `server_error`; 402 payment required (prepaid
  credit at zero, spending limit reached, failed invoice); 429 rate limit of the
  subscription tier (requests per second, tokens per minute, concurrent requests); 503
  temporarily overloaded or under maintenance.
- Scaleway, [understanding errors](https://www.scaleway.com/en/docs/generative-apis/api-cli/understanding-errors/):
  nested `error` object for recent models, flat object for older ones, numeric `code`;
  403 unknown key or missing permission; 429 "Too Many Requests" and "Too Many Tokens"
  (quota per minute).
  [Fixing common issues](https://www.scaleway.com/en/docs/generative-apis/troubleshooting/fixing-common-issues/):
  423 model locked (no valid payment method, or a consumption limit set to zero); 429 for
  requests or tokens per minute and for concurrent requests.

Neither provider documents the identifier it sends when it has no capacity left, and
neither documents `insufficient_quota`-style identifiers: those substrings follow the usual
naming of OpenAI-compatible endpoints and are confirmed only once such a refusal appears in
the logs. The logged `type` and `code` show what the provider really sent. Observed on
2026-11-12 by the owner's watch probe (one-token request): Mistral answered HTTP 429 with
`type` `backend_out_of_capacity`, which is classified `capacity`.
