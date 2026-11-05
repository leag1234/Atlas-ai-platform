import csv
import re
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

STATUSES = ("paid", "refunded", "cancelled")


def money(value):
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    units, frac = f"{abs(q):.2f}".split(".")
    return f"{sign}{int(units):,}.{frac}"


def parse(row):
    if len(row) != 7:
        return None
    order_id, day, region, product, quantity, price, status = row
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.strip()):
        return None
    try:
        day = date.fromisoformat(day.strip())
        quantity = int(quantity.strip())
        price = Decimal(price.strip().replace(",", ""))
    except (ValueError, InvalidOperation):
        return None
    status = status.strip().lower()
    if quantity < 1 or not price.is_finite() or price < 0 or status not in STATUSES:
        return None
    return order_id, day, region.strip(), product.strip(), quantity, price, status


def main(argv):
    if len(argv) != 1:
        print("usage: report.py FILE.csv", file=sys.stderr)
        return 1
    try:
        with open(argv[0], newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    well, skipped, last = 0, 0, {}
    for row in rows[1:]:
        if not row:
            continue
        parsed = parse(row)
        if parsed is None:
            skipped += 1
            continue
        well += 1
        last.pop(parsed[0], None)
        last[parsed[0]] = parsed
    counted = [r for r in last.values() if r[6] != "cancelled"]
    regions, products = {}, {}
    for _, day, region, product, quantity, price, status in counted:
        amount = quantity * price * (1 if status == "paid" else -1)
        key = region.lower()
        entry = regions.setdefault(key, [region.title(), 0, Decimal(0)])
        entry[1] += status == "paid"
        entry[2] += amount
        products[product] = products.get(product, Decimal(0)) + amount
    out = ["# Sales report", ""]
    if counted:
        out.append(f"Period: {min(r[1] for r in counted)} to {max(r[1] for r in counted)}")
    else:
        out.append("Period: none")
    out += [f"Well-formed rows: {well}", f"Skipped rows: {skipped}", "", "| Region | Orders | Revenue |", "|---|---:|---:|"]
    for name, orders, revenue in sorted(regions.values(), key=lambda e: (-e[2], e[0])):
        out.append(f"| {name} | {orders} | {money(revenue)} |")
    out.append(f"| **Total** | {sum(e[1] for e in regions.values())} | {money(sum((e[2] for e in regions.values()), Decimal(0)))} |")
    out.append("")
    if products:
        top = sorted(products.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out.append(f"Top product: {top[0]} ({money(top[1])})")
    else:
        out.append("Top product: none")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
