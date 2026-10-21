"""Command line interface for the stock list."""
import argparse
import sys

DEFAULT_ITEMS = [
    {"name": "washer", "qty": 5},
    {"name": "bolt", "qty": 120},
    {"name": "nut", "qty": 80},
]


def build_parser():
    parser = argparse.ArgumentParser(prog="inventory")
    parser.add_argument("--min-qty", type=int, default=0,
                        help="only list items with at least this quantity")
    return parser


def main(argv=None, stdout=None, items=None):
    stdout = sys.stdout if stdout is None else stdout
    items = DEFAULT_ITEMS if items is None else items
    args = build_parser().parse_args(argv)
    for item in sorted(items, key=lambda i: i["name"]):
        if item["qty"] >= args.min_qty:
            print(f"{item['name']}: {item['qty']}", file=stdout)
    return 0
