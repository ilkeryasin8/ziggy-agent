"""Command line interface for the stock list."""
import argparse
import json
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
    parser.add_argument("--json", action="store_true",
                        help="print a single JSON object instead of text")
    return parser


def main(argv=None, stdout=None, items=None):
    stdout = sys.stdout if stdout is None else stdout
    items = DEFAULT_ITEMS if items is None else items
    args = build_parser().parse_args(argv)
    rows = [i for i in sorted(items, key=lambda i: i["name"]) if i["qty"] >= args.min_qty]
    if args.json:
        payload = {"items": rows, "total": sum(r["qty"] for r in rows)}
        print(json.dumps(payload), file=stdout)
        return 0
    for item in rows:
        print(f"{item['name']}: {item['qty']}", file=stdout)
    return 0
