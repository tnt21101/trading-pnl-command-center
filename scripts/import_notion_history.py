#!/usr/bin/env python3
"""Import existing Notion Daily PnL calendar rows into local dashboard history.

This is a read-only Notion import. It writes:
- data/history.json

It keeps the dashboard independent from Notion going forward while preserving the
historical P&L cards already stored there.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
DEFAULT_DATA_SOURCE_ID = "1f7b75ad-f1f0-81e0-91b5-000b68f1cb13"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "history.json"
DEFAULT_START_DATE = "2026-06-01"


def token() -> str:
    value = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
    if not value:
        raise RuntimeError("Missing NOTION_API_KEY/NOTION_TOKEN")
    return value


def headers() -> dict:
    return {
        "Authorization": f"Bearer {token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def request_json(method: str, path: str, payload: dict | None = None, attempts: int = 6):
    url = f"{NOTION_API}/{path.lstrip('/')}"
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers(), method=method)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < attempts - 1:
                time.sleep(float(exc.headers.get("Retry-After") or 1.5))
                continue
            detail = exc.read().decode(errors="ignore")
            raise RuntimeError(f"Notion {method} {path} failed: {exc.code} {detail}") from exc


def rich_text_to_plain(items):
    if not items:
        return ""
    return "".join((item.get("plain_text") or item.get("text", {}).get("content") or "") for item in items)


def title_to_plain(prop):
    return rich_text_to_plain(prop.get("title", []))


def number_from_title(text: str):
    if not text:
        return None
    # Handles +$3,786.15, -$11,981.38, $0.00
    m = re.search(r"([+-])?\$?\s*([0-9][0-9,]*(?:\.\d+)?)", text)
    if not m:
        return None
    val = float(m.group(2).replace(",", ""))
    if m.group(1) == "-":
        val = -val
    return val


def pick_props(schema: dict):
    props = schema.get("properties", {})

    def first_type(t):
        for name, prop in props.items():
            if prop.get("type") == t:
                return name
        return None

    title = first_type("title") or "Name"
    date = None
    for name, prop in props.items():
        if prop.get("type") == "date" and (date is None or "date" in name.lower()):
            date = name
    pnl = next((n for n, p in props.items() if p.get("type") == "number" and any(s in n.lower() for s in ["pnl", "p&l", "profit", "realized"])), None)
    fees = next((n for n, p in props.items() if p.get("type") == "number" and "fee" in n.lower()), None)
    fills = next((n for n, p in props.items() if p.get("type") == "number" and "fill" in n.lower()), None)
    volume = next((n for n, p in props.items() if p.get("type") == "number" and ("volume" in n.lower() or n.lower() == "vol")), None)
    trade_details = next((n for n, p in props.items() if p.get("type") == "rich_text" and "trade" in n.lower()), None)
    brandon = next((n for n, p in props.items() if p.get("type") == "rich_text" and "brandon" in n.lower()), None)
    learned = next((n for n, p in props.items() if p.get("type") == "rich_text" and "learn" in n.lower()), None)
    notes = next((n for n, p in props.items() if p.get("type") == "rich_text" and any(s in n.lower() for s in ["note", "summary", "top", "description"])), None)
    result = next((n for n, p in props.items() if p.get("type") == "select" and any(s in n.lower() for s in ["result", "status", "day"])), None)
    return {
        "title": title,
        "date": date,
        "pnl": pnl,
        "fees": fees,
        "fills": fills,
        "volume": volume,
        "trade_details": trade_details,
        "brandon_insight": brandon,
        "what_i_learned": learned,
        "notes": notes,
        "result": result,
    }


def get_property(page: dict, name: str | None):
    if not name:
        return None
    return page.get("properties", {}).get(name)


def extract_page(page: dict, propmap: dict):
    title_prop = get_property(page, propmap["title"]) or {}
    title = title_to_plain(title_prop)
    date_prop = get_property(page, propmap["date"]) or {}
    date_value = (date_prop.get("date") or {}).get("start")

    def number(name):
        prop = get_property(page, name) or {}
        return prop.get("number")

    def text(name):
        prop = get_property(page, name) or {}
        return rich_text_to_plain(prop.get("rich_text", []))

    pnl = number(propmap["pnl"])
    if pnl is None:
        pnl = number_from_title(title)
    result_prop = get_property(page, propmap["result"]) or {}
    result = (result_prop.get("select") or {}).get("name")
    if not result:
        result = "Win" if (pnl or 0) > 0 else "Loss" if (pnl or 0) < 0 else "Flat"

    return {
        "page_id": page.get("id"),
        "date": date_value,
        "title": title,
        "realized_after_fees": pnl,
        "fees": number(propmap["fees"]),
        "fills": number(propmap["fills"]),
        "volume": number(propmap["volume"]),
        "result": result,
        "trade_details": text(propmap["trade_details"]),
        "brandon_insight": text(propmap["brandon_insight"]),
        "what_i_learned": text(propmap["what_i_learned"]),
        "notes": text(propmap["notes"]),
        "source": "notion",
    }


def query_all(data_source_id: str, date_prop: str | None):
    rows = []
    cursor = None
    payload_base = {"page_size": 100}
    if date_prop:
        payload_base["sorts"] = [{"property": date_prop, "direction": "ascending"}]
    while True:
        payload = dict(payload_base)
        if cursor:
            payload["start_cursor"] = cursor
        res = request_json("POST", f"data_sources/{data_source_id}/query", payload)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def main():
    data_source_id = os.getenv("NOTION_DAILY_PNL_DATABASE_ID") or DEFAULT_DATA_SOURCE_ID
    schema = request_json("GET", f"data_sources/{data_source_id}")
    propmap = pick_props(schema)
    pages = query_all(data_source_id, propmap["date"])
    start_date = os.getenv("NOTION_HISTORY_START_DATE", DEFAULT_START_DATE)
    end_date = os.getenv("NOTION_HISTORY_END_DATE")
    items = [extract_page(page, propmap) for page in pages]
    items = [item for item in items if item.get("date")]
    if start_date:
        items = [item for item in items if item["date"] >= start_date]
    if end_date:
        items = [item for item in items if item["date"] <= end_date]
    items.sort(key=lambda item: item["date"])

    totals = {
        "days": len(items),
        "realized_after_fees": sum(float(item.get("realized_after_fees") or 0) for item in items),
        "fees": sum(float(item.get("fees") or 0) for item in items if item.get("fees") is not None),
        "fills": int(sum(float(item.get("fills") or 0) for item in items if item.get("fills") is not None)),
    }
    wins = [item for item in items if (item.get("realized_after_fees") or 0) > 0]
    losses = [item for item in items if (item.get("realized_after_fees") or 0) < 0]
    payload = {
        "source": "notion",
        "data_source_id": data_source_id,
        "propmap": propmap,
        "imported_at": dt.datetime.now().isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "totals": {**totals, "wins": len(wins), "losses": len(losses)},
        "days": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Imported {len(items)} Notion PnL days into {OUT}")
    print(json.dumps(payload["totals"], indent=2))


if __name__ == "__main__":
    main()
