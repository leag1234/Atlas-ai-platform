import argparse

from shipping import config
from shipping.invoice import render


def main(argv=None):
    parser = argparse.ArgumentParser(prog="shipping")
    parser.add_argument("order_id", type=int)
    parser.add_argument("weight", type=float)
    parser.add_argument("total")
    parser.add_argument("--settings")
    args = parser.parse_args(argv)
    if args.settings:
        config.load(args.settings)
    print(render(args.order_id, args.weight, args.total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
