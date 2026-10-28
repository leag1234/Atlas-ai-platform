# API keys UI polish report

## Result

Updated the API keys dialog on `keys-ui-polish`.

- The row revoke action is a compact outlined danger action, right aligned and not full width on phone. The confirmation dialog remains destructive and explicit.
- Phone rows now show localized Created, Last used, and Expires labels plus the current-day usage and quota line.
- Revocation no longer creates the black status pill; the greyed revoked row carries its localized status.
- Active keys sort ahead of revoked keys. Revoked keys remain visible and labelled.
- Added the missing shared locale-table entries for Spanish and Italian so all five supported languages resolve the added labels and existing usage labels.

## Evidence

- `ATLAS_PORT_BASE=26100 PYTHONPATH=.:tests:services/model-gateway python -m unittest tests.test_api_key_ui -v` — PASS, 8 tests.
- `ATLAS_PORT_BASE=26100 make lint` — PASS.
- `ATLAS_PORT_BASE=26100 make typecheck` — PASS.
- `ATLAS_PORT_BASE=26100 make verify-m16` — PASS.
- `git diff --check` — PASS.
- Requested local-stack screenshot capture was not run: this worktree does not have the browser-stack environment variables required by `tests/api_keys_browser.py`. No visual-review result is claimed.

ASSUMPTION: supplied supervisor captures were read-only review references; all edits were confined to this worktree.

RISK: another task's `make test` and then `make test-journeys` were active. I waited until `pgrep -x make` cleared before running the checks above.

Source: existing `api-key-ui.js` locale table and API-key browser test structure.

## Cost

No live model calls or cloud resources. Cost: 0 EUR.
