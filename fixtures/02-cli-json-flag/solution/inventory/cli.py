"""Inventory command-line interface."""
import argparse
import json
import sys

from .data import load_items


def format_table(items):
    lines = [f"{'SKU':<8} {'NAME':<20} {'QTY':>5}"]
    for it in sorted(items, key=lambda i: i["sku"]):
        lines.append(f"{it['sku']:<8} {it['name']:<20} {it['qty']:>5}")
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(prog="inventory")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="print stock report")
    report.add_argument("--min-qty", type=int, default=0, help="only items with at least this quantity")
    report.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def main(argv=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    items = [i for i in load_items() if i["qty"] >= args.min_qty]
    if args.command == "report":
        if args.json:
            rows = [{k: i[k] for k in ("sku", "name", "qty")} for i in items]
            print(json.dumps(sorted(rows, key=lambda r: r["sku"])), file=out)
        else:
            print(format_table(items), file=out)
    return 0
