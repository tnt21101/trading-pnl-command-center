# Trading P&L Command Center

Private, local-first trading cockpit for Hyperliquid P&L, open risk, replay analytics, and daily journaling.

## What it shows

- Manual and managed Hyperliquid accounts kept separate
- Closed after-fees P&L, open/unrealized P&L, net marked P&L, fees, and account value
- P&L by ticker and open-position risk table
- Replay-the-day leak detector with trade clusters, peak/giveback, event timeline, and behavior grades
- Compact learning journal with local browser storage
- Optional backfilled monthly P&L calendar

## Files

- `index.html` — standalone compact dashboard UI
- `server.py` — local HTTP/API server with `/api/latest` and `/api/refresh`
- `scripts/fetch_hl_pnl.py` — read-only Hyperliquid collector that writes `data/latest.json`
- `scripts/import_notion_history.py` — optional Notion import for historical P&L rows
- `data/.gitkeep` — keeps the data directory in git without committing account snapshots

## Privacy / repo hygiene

Runtime JSON snapshots are intentionally ignored:

- `data/latest.json`
- `data/history.json`

Those files can contain account addresses, fills, positions, fees, and historical P&L. Regenerate them locally instead of committing them.

## Run locally

```bash
cd /root/trading-dashboard
python3 scripts/fetch_hl_pnl.py
python3 server.py --host 0.0.0.0 --port 8765
```

Open:

```text
http://127.0.0.1:8765/index.html
```

If `data/latest.json` is missing, the dashboard uses built-in prototype data until the collector runs.

## P&L rules

- Closed before fees = sum of `closedPnl`
- Fees = sum of `fee`
- Closed after fees = closed P&L minus fees
- Open/unrealized = current `unrealizedPnl` on open positions
- Net marked now = closed after fees plus open/unrealized
- Both default Hyperliquid perp universe and `xyz` dex are checked
