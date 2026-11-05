# Sales report specification

Usage: `python3 report.py FILE.csv` writes the report to standard output and exits 0. If the file cannot be
read, print an error to standard error and exit 1.

## Input

UTF-8 CSV with the header `order_id,date,region,product,quantity,unit_price,status`, parsed with standard CSV
quoting rules. Completely blank lines are ignored and counted nowhere.

A data row is **well-formed** when all of the following hold, otherwise it is **skipped**:
- it has exactly 7 fields;
- `date` is a valid `YYYY-MM-DD` date;
- `quantity` is an integer >= 1 (surrounding spaces allowed);
- `unit_price` is a non-negative decimal number; it may contain `,` thousands separators (`"1,299.00"`);
- `status`, ignoring case and surrounding spaces, is `paid`, `refunded` or `cancelled`.

`region` and `product` are stripped of surrounding spaces. Regions are grouped case-insensitively and displayed
with `str.title()`. Products are grouped exactly as written after stripping.

If several well-formed rows share an `order_id`, only the last one in the file counts; the earlier ones are
ignored (they are still well-formed rows). Rows with status `cancelled` do not count.

For each counted row, `amount = quantity * unit_price` computed with `decimal.Decimal`; it is positive for
`paid` and negative for `refunded`.

## Output

```
# Sales report

Period: <first date> to <last date>
Well-formed rows: <number of well-formed rows>
Skipped rows: <number of skipped rows>

| Region | Orders | Revenue |
|---|---:|---:|
| <region> | <paid orders> | <revenue> |
| **Total** | <paid orders> | <revenue> |

Top product: <product> (<revenue>)
```

- Period uses the smallest and largest date of the counted rows.
- One table row per region that has at least one counted row. `Orders` is the number of counted `paid` rows;
  `Revenue` is the sum of amounts (refunds included). Rows are sorted by revenue descending, then by displayed
  region name ascending.
- The top product is the one with the largest summed amount over all regions; ties go to the alphabetically
  first product name.
- Money is printed with two decimals (rounding half up), `,` thousands separators and a leading `-` when
  negative, e.g. `-1,234.50`.
- When no row counts, print `Period: none`, only the total row (`| **Total** | 0 | 0.00 |`) and
  `Top product: none`.
- Every line ends with `\n`, including the last one; there is no other trailing whitespace.
