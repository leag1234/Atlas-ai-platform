import argparse
import sys

from inventory.model import load


def render_text(items):
    lines = [f"{'SKU':<8}{'NAME':<20}{'QTY':>6}{'VALUE':>12}"]
    total = 0
    for item in sorted(items, key=lambda i: i.sku):
        lines.append(f"{item.sku:<8}{item.name:<20}{item.quantity:>6}{item.value:>12}")
        total += item.value
    lines.append(f"{'TOTAL':<34}{total:>12}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="inventory")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="print a stock valuation")
    report.add_argument("path")
    report.add_argument("--min-quantity", type=int, default=0)
    args = parser.parse_args(argv)
    items = [i for i in load(args.path) if i.quantity >= args.min_quantity]
    sys.stdout.write(render_text(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
