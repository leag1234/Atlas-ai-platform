import argparse

from shipping.config import Settings, load_settings
from shipping.invoice import render


def main(argv=None):
    parser = argparse.ArgumentParser(prog="shipping")
    parser.add_argument("order_id", type=int)
    parser.add_argument("weight", type=float)
    parser.add_argument("total")
    parser.add_argument("--settings")
    args = parser.parse_args(argv)
    settings = load_settings(args.settings) if args.settings else Settings()
    print(render(args.order_id, args.weight, args.total, settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
