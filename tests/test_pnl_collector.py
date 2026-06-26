import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_hl_pnl.py"
spec = importlib.util.spec_from_file_location("fetch_hl_pnl", MODULE_PATH)
hl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hl)


class PnlCollectorTests(unittest.TestCase):
    def test_fetch_fills_splits_capped_windows_and_dedupes(self):
        original_post = hl.post
        calls = []
        duplicate = {"time": 1, "coin": "BTC", "oid": 1, "dir": "Close Long", "sz": "1", "px": "100", "closedPnl": "10", "fee": "1"}
        right = {"time": 2, "coin": "BTC", "oid": 2, "dir": "Close Long", "sz": "1", "px": "101", "closedPnl": "20", "fee": "1"}

        def fake_post(payload):
            calls.append((payload["startTime"], payload["endTime"]))
            if len(calls) == 1:
                return [duplicate.copy() for _ in range(1900)]
            if len(calls) == 2:
                return [duplicate]
            return [right]

        try:
            hl.post = fake_post
            fills = hl.fetch_fills_by_time("0xabc", 0, 100_000)
        finally:
            hl.post = original_post

        self.assertGreater(len(calls), 1)
        self.assertEqual(len(fills), 2)
        self.assertEqual(sum(float(f["closedPnl"]) - float(f["fee"]) for f in fills), 28.0)

    def test_history_upsert_overwrites_stale_rows_and_recomputes_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps({"source": "notion", "days": [
                {"date": "2026-06-25", "realized_after_fees": 1, "fees": 1, "fills": 1, "source": "notion"}
            ]}))
            history = hl.upsert_history_rows(path, [
                {"date": "2026-06-25", "realized_after_fees": 12829.58, "fees": 3068.41, "fills": 2712},
                {"date": "2026-06-26", "realized_after_fees": -2.74, "fees": 2.74, "fills": 9},
            ], start_date="2026-06-01")

        self.assertEqual(history["source"], "hyperliquid")
        self.assertEqual(history["days"][0]["realized_after_fees"], 12829.58)
        self.assertEqual(history["days"][0]["source"], "hyperliquid")
        self.assertEqual(history["totals"]["fills"], 2721)
        self.assertAlmostEqual(history["totals"]["realized_after_fees"], 12826.84)

    def test_fetch_account_open_pnl_sums_default_and_xyz_positions_once(self):
        original_post = hl.post
        responses = {
            "userRole": {"role": "user"},
            "userFillsByTime": [],
            "spotClearinghouseState": {"balances": [], "tokenToAvailableAfterMaintenance": []},
            "frontendOpenOrders": [],
        }

        def fake_post(payload):
            typ = payload["type"]
            if typ == "clearinghouseState":
                if payload.get("dex") == "xyz":
                    return {"marginSummary": {"accountValue": "200"}, "assetPositions": [
                        {"position": {"coin": "xyz:MSTR", "szi": "-2", "entryPx": "85", "positionValue": "160", "unrealizedPnl": "10", "liquidationPx": "95", "leverage": {}}}
                    ]}
                return {"marginSummary": {"accountValue": "100"}, "assetPositions": [
                    {"position": {"coin": "BTC", "szi": "1", "entryPx": "100", "positionValue": "110", "unrealizedPnl": "5", "liquidationPx": "50", "leverage": {}}}
                ]}
            return responses[typ]

        try:
            hl.post = fake_post
            account = hl.fetch_account("test", "0xabc", 0, 1000)
        finally:
            hl.post = original_post

        self.assertEqual(account["open_unrealized"], 15.0)
        self.assertEqual(len(account["positions"]), 2)
        self.assertEqual(account["net_marked_now"], 15.0)


if __name__ == "__main__":
    unittest.main()
