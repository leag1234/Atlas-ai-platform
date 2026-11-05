`ledger.py` has grown into a single module that mixes the data model, parsing and reporting. Split it into a
package without changing behaviour:

- `ledger/models.py`: `Entry`, `Account`, `LedgerError`
- `ledger/parse.py`: `parse_amount`, `parse_line`, `parse_file`
- `ledger/report.py`: `balances`, `format_report`, `monthly_totals`
- `ledger/__init__.py`: re-exports exactly these nine public names (and `__all__` lists them)

Each name must be defined in the module listed above (not merely imported there), `ledger.py` must be
removed, `python3 -m ledger.report FILE` must keep working like `python3 ledger.py FILE` did, and the
existing tests must pass unchanged.
