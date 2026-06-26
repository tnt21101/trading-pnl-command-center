#!/usr/bin/env python3
"""Rebuild local dashboard PnL history from Hyperliquid fills.

This is the correction-safe source-of-truth repair tool for the P&L calendar.
It overwrites stale imported/capped rows with fill-derived CT-day totals.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import fetch_hl_pnl as hl


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", choices=["manual", "managed"], default="manual")
    parser.add_argument("--start", required=True, help="CT start date YYYY-MM-DD")
    parser.add_argument("--end", help="CT end date YYYY-MM-DD, default today CT")
    parser.add_argument("--out", help="history JSON path, default data/history.json or data/managed_history.json")
    return parser.parse_args()


def daterange(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def main():
    args = parse_args()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(hl.CT).date()
    address = hl.MANUAL if args.account == "manual" else hl.load_managed_address()
    default_out = hl.DATA_DIR / ("history.json" if args.account == "manual" else "managed_history.json")
    out = Path(args.out) if args.out else default_out
    rows = []
    for day in daterange(start, end):
        row = hl.fetch_day_row(address, day)
        rows.append(row)
        print(f"{row['date']} {row['realized_after_fees']:+,.2f} fills={row['fills']}")
    history = hl.upsert_history_rows(out, rows, start_date=start.isoformat())
    print(json.dumps({"wrote": str(out), "rows_upserted": len(rows), "totals": history.get("totals")}, indent=2))


if __name__ == "__main__":
    main()
