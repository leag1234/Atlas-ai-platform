The `shipping` package reads a module-level mutable dict `shipping.config.CONFIG` from several modules, which
makes it impossible to price two carriers with different settings in the same process. Refactor:

- In `shipping/config.py`, replace `CONFIG` with a frozen dataclass `Settings` with fields
  `currency: str = "EUR"`, `base_fee: Decimal = Decimal("4.90")`, `per_kg: Decimal = Decimal("1.20")`,
  `free_above: Decimal | None = Decimal("60.00")`, `label_prefix: str = "SHP"`, and a function
  `load_settings(path)` that reads a JSON file with any subset of these keys (amounts as strings) and returns a
  `Settings`; unknown keys raise `ValueError`.
- New signatures: `rates.quote(weight_kg, order_total, settings)`, `labels.make_label(order_id, settings)`,
  `invoice.render(order_id, weight_kg, order_total, settings)`.
- `python3 -m shipping.cli ORDER_ID WEIGHT TOTAL [--settings FILE]` keeps its output format; without
  `--settings` it uses `Settings()`.
- No module of the package may define or read a module-level settings object any more: the name `CONFIG` must
  disappear from the package, and the functions must not keep settings in globals.
- Update the existing tests to the new signatures without weakening them.
