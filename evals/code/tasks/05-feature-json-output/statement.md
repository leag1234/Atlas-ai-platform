Add a `--format` option to the `report` command of the inventory CLI (`python3 -m inventory.cli`).

- `--format text` (the default) keeps the current output byte for byte.
- `--format json` prints `json.dumps(document, indent=2)` followed by a newline, where `document` is
  `{"items": [...], "total_value": "<amount>"}`.
  Each item is `{"sku": str, "name": str, "quantity": int, "value": "<amount>"}`, items sorted by `sku`.
  `value` is quantity times unit price. Amounts are decimal strings with exactly two decimals
  (round half up), computed with `decimal.Decimal`, never floats.
- `--min-quantity N` already filters items for the text format; it must apply to JSON as well, and
  `total_value` covers only the items that are printed.
- Any other `--format` value must make argparse exit with status 2.
