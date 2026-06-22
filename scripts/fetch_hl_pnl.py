#!/usr/bin/env python3
"""Fetch Tim manual + managed Hyperliquid P&L into data/latest.json.

Read-only: uses Hyperliquid Info API only. No private keys required.
"""
import datetime as dt
import json
import os
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT = DATA_DIR / "latest.json"
MANUAL = "0x96abd7547C7ef5A0C4F2bF04DCD74Dd96A461b56"
MANAGED_FALLBACK = "0xaf94bd422310674ECa7475239b9e515A198e5048"
CT = ZoneInfo("America/Chicago")


def post(payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode())


def resolve_user(address: str):
    role = post({"type": "userRole", "user": address})
    if isinstance(role, dict) and role.get("role") == "agent":
        return role.get("data", {}).get("user", address), role
    return address, role


def load_managed_address():
    main = None
    agent = None
    for path in ["/root/.hermes/secrets/hyperliquid-trading.env", "/root/.hermes/secrets/hyperliquid.env"]:
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if key in {"HYPERLIQUID_MAIN_ADDRESS", "HYPERLIQUID_MANAGED_MAIN_ADDRESS"}:
                    main = value
                if key in {"HYPERLIQUID_AGENT_ADDRESS", "HYPERLIQUID_MANAGED_AGENT_ADDRESS"}:
                    agent = value
    return main or agent or MANAGED_FALLBACK


def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def analyze_replay(fills: list[dict], open_unrealized: float = 0.0) -> dict:
    """Create a trade-day replay and leak detector from raw fills.

    This is intentionally heuristic: Hyperliquid fills do not directly label each
    discretionary trade, so we cluster by ticker and time proximity.
    """
    if not fills:
        return {
            "peak_realized": 0.0,
            "end_realized": 0.0,
            "current_marked": open_unrealized,
            "giveback_from_peak": 0.0,
            "leak_score": "No trades",
            "grade": "—",
            "events": [],
            "clusters": [],
            "coach_read": "No fills today. Sitting out is valid when no clean setup exists.",
            "scores": {},
        }

    ordered = sorted(fills, key=lambda f: int(f.get("time") or 0))
    cumulative = 0.0
    peak = -10**18
    peak_time = None
    low_after_peak = None
    events = []
    by_coin = {}
    fee_total = 0.0

    for f in ordered:
        t = int(f.get("time") or 0)
        net = safe_float(f.get("closedPnl")) - safe_float(f.get("fee"))
        fee = safe_float(f.get("fee"))
        fee_total += fee
        cumulative += net
        coin = f.get("coin", "?")
        rec = by_coin.setdefault(coin, {"net": 0.0, "fees": 0.0, "fills": 0, "first": t, "last": t, "volume": 0.0})
        rec["net"] += net
        rec["fees"] += fee
        rec["fills"] += 1
        rec["first"] = min(rec["first"], t)
        rec["last"] = max(rec["last"], t)
        rec["volume"] += abs(safe_float(f.get("sz")) * safe_float(f.get("px")))
        if cumulative > peak:
            peak = cumulative
            peak_time = t
            low_after_peak = cumulative
        elif peak_time is not None:
            low_after_peak = min(low_after_peak, cumulative) if low_after_peak is not None else cumulative

    end_realized = cumulative
    current_marked = end_realized + open_unrealized
    giveback = max(0.0, peak - current_marked) if peak > -10**17 else 0.0

    # Time-cluster fills by coin with <= 15 min gaps.
    clusters = []
    current = None
    max_gap_ms = 15 * 60 * 1000
    for f in ordered:
        t = int(f.get("time") or 0)
        coin = f.get("coin", "?")
        net = safe_float(f.get("closedPnl")) - safe_float(f.get("fee"))
        fee = safe_float(f.get("fee"))
        volume = abs(safe_float(f.get("sz")) * safe_float(f.get("px")))
        if not current or current["coin"] != coin or t - current["last_ms"] > max_gap_ms:
            if current:
                clusters.append(current)
            current = {"coin": coin, "start_ms": t, "last_ms": t, "net": 0.0, "fees": 0.0, "fills": 0, "volume": 0.0, "dirs": {}}
        current["last_ms"] = t
        current["net"] += net
        current["fees"] += fee
        current["fills"] += 1
        current["volume"] += volume
        d = f.get("dir", "?")
        current["dirs"][d] = current["dirs"].get(d, 0) + 1
    if current:
        clusters.append(current)

    clusters.sort(key=lambda c: abs(c["net"]), reverse=True)
    top_cluster = max(clusters, key=lambda c: c["net"], default=None)
    worst_cluster = min(clusters, key=lambda c: c["net"], default=None)
    top_coin = max(by_coin.items(), key=lambda kv: kv[1]["net"], default=(None, None))
    worst_coin = min(by_coin.items(), key=lambda kv: kv[1]["net"], default=(None, None))

    fill_count = len(fills)
    fee_drag_pct = (fee_total / max(abs(end_realized) + fee_total, 1.0)) * 100
    giveback_pct = (giveback / max(abs(peak), 1.0)) * 100 if peak else 0.0
    if giveback_pct >= 35 or fill_count >= 1500 or fee_drag_pct >= 20:
        leak_score = "High"
    elif giveback_pct >= 15 or fill_count >= 700 or fee_drag_pct >= 10:
        leak_score = "Moderate"
    else:
        leak_score = "Low"

    setup_quality = "A" if end_realized > 0 and fill_count < 700 else "B" if end_realized > 0 else "C"
    peak_protection = "A" if giveback_pct < 10 else "B" if giveback_pct < 20 else "C" if giveback_pct < 35 else "D"
    fee_control = "A" if fee_drag_pct < 4 else "B" if fee_drag_pct < 8 else "C" if fee_drag_pct < 14 else "D"
    overtrade_control = "A" if fill_count < 400 else "B" if fill_count < 800 else "C" if fill_count < 1400 else "D"
    grade = "A" if leak_score == "Low" and end_realized > 0 else "B" if end_realized > 0 and leak_score != "High" else "C" if end_realized > 0 else "D"

    def ct_time(ms):
        return dt.datetime.fromtimestamp(ms / 1000, CT).strftime("%-I:%M %p")

    events.append({"time": ct_time(ordered[0].get("time") or 0), "kind": "start", "text": f"First fill: {ordered[0].get('coin')}"})
    if top_cluster:
        events.append({"time": ct_time(top_cluster["last_ms"]), "kind": "best", "text": f"Best cluster: {top_cluster['coin']} {money(top_cluster['net'])} net across {top_cluster['fills']} fills"})
    if worst_cluster and worst_cluster["net"] < 0:
        events.append({"time": ct_time(worst_cluster["last_ms"]), "kind": "leak", "text": f"Worst leak: {worst_cluster['coin']} {money(worst_cluster['net'])} net"})
    if peak_time:
        events.append({"time": ct_time(peak_time), "kind": "peak", "text": f"Realized P&L high-water mark hit {money(peak)}"})
    if fee_total >= 100:
        events.append({"time": ct_time(ordered[-1].get("time") or 0), "kind": "fees", "text": f"Fees crossed meaningful level: -${fee_total:,.2f}"})
    if giveback > 0:
        events.append({"time": ct_time(ordered[-1].get("time") or 0), "kind": "giveback", "text": f"Giveback from realized peak to marked-now: -${giveback:,.2f}"})

    cluster_out = []
    for c in clusters[:10]:
        cluster_out.append({
            "coin": c["coin"], "start": ct_time(c["start_ms"]), "end": ct_time(c["last_ms"]),
            "net": c["net"], "fees": c["fees"], "fills": c["fills"], "volume": c["volume"],
            "dirs": ", ".join(f"{k}:{v}" for k, v in sorted(c["dirs"].items())),
        })

    coach_bits = []
    if top_coin[0]:
        coach_bits.append(f"Best payer was {top_coin[0]} at {money(top_coin[1]['net'])} net.")
    if worst_coin[0] and worst_coin[1]["net"] < 0:
        coach_bits.append(f"Main leak was {worst_coin[0]} at {money(worst_coin[1]['net'])} net.")
    if giveback_pct >= 15:
        coach_bits.append(f"Protect the peak better: marked-now is {giveback_pct:.0f}% below the realized high-water mark.")
    if fill_count >= 700:
        coach_bits.append(f"Fill count is high ({fill_count}); watch overtrading and fee drag after the clean move.")
    if not coach_bits:
        coach_bits.append("Clean day profile: low leak score, keep using the same trigger → partial → runner process.")

    return {
        "peak_realized": peak,
        "peak_time": ct_time(peak_time) if peak_time else None,
        "end_realized": end_realized,
        "current_marked": current_marked,
        "giveback_from_peak": giveback,
        "giveback_pct": giveback_pct,
        "fee_drag_pct": fee_drag_pct,
        "leak_score": leak_score,
        "grade": grade,
        "events": events,
        "clusters": cluster_out,
        "coach_read": " ".join(coach_bits),
        "scores": {
            "made_money": "A" if end_realized > 0 else "D",
            "protected_peak": peak_protection,
            "setup_quality": setup_quality,
            "overtrading_control": overtrade_control,
            "fee_control": fee_control,
        },
    }


def fetch_account(label: str, address: str, start_ms: int, end_ms: int):
    main, role = resolve_user(address)
    fills = post({"type": "userFillsByTime", "user": main, "startTime": start_ms, "endTime": end_ms, "aggregateByTime": False})
    if not isinstance(fills, list):
        fills = []

    closed = sum(safe_float(f.get("closedPnl")) for f in fills)
    fees = sum(safe_float(f.get("fee")) for f in fills)
    volume = sum(abs(safe_float(f.get("sz")) * safe_float(f.get("px"))) for f in fills)

    by_coin = defaultdict(lambda: {"closed": 0.0, "fees": 0.0, "fills": 0, "volume": 0.0})
    for fill in fills:
        coin = fill.get("coin", "?")
        by_coin[coin]["closed"] += safe_float(fill.get("closedPnl"))
        by_coin[coin]["fees"] += safe_float(fill.get("fee"))
        by_coin[coin]["fills"] += 1
        by_coin[coin]["volume"] += abs(safe_float(fill.get("sz")) * safe_float(fill.get("px")))

    positions = []
    account_values = {"perp_default": 0.0, "perp_xyz": 0.0, "spot_usdc": 0.0, "spot_total": 0.0}
    margin_summaries = {}
    spot_balances = []
    for dex in [None, "xyz"]:
        payload = {"type": "clearinghouseState", "user": main}
        if dex:
            payload["dex"] = dex
        state = post(payload)
        dex_key = dex or "default"
        if isinstance(state, dict):
            margin_summaries[dex_key] = state.get("marginSummary", {})
            account_values["perp_xyz" if dex == "xyz" else "perp_default"] = safe_float(
                state.get("marginSummary", {}).get("accountValue")
            )
        for asset_position in state.get("assetPositions", []) if isinstance(state, dict) else []:
            pos = asset_position.get("position", {})
            szi = safe_float(pos.get("szi"))
            if abs(szi) <= 0:
                continue
            positions.append({
                "dex": dex or "default",
                "coin": pos.get("coin"),
                "side": "long" if szi > 0 else "short",
                "szi": szi,
                "entryPx": pos.get("entryPx"),
                "positionValue": safe_float(pos.get("positionValue")),
                "unrealizedPnl": safe_float(pos.get("unrealizedPnl")),
                "liq": pos.get("liquidationPx"),
                "lev": pos.get("leverage"),
            })

    try:
        spot_state = post({"type": "spotClearinghouseState", "user": main})
    except Exception:
        spot_state = {}
    for balance in spot_state.get("balances", []) if isinstance(spot_state, dict) else []:
        total = safe_float(balance.get("total"))
        if total <= 0:
            continue
        coin = balance.get("coin")
        spot_balances.append({"coin": coin, "total": total, "hold": safe_float(balance.get("hold"))})
        account_values["spot_total"] += total
        if coin == "USDC":
            account_values["spot_usdc"] += total

    orders = []
    for dex in [None, "xyz"]:
        payload = {"type": "frontendOpenOrders", "user": main}
        if dex:
            payload["dex"] = dex
        try:
            open_orders = post(payload)
        except Exception:
            open_orders = []
        if not isinstance(open_orders, list):
            continue
        for order in open_orders:
            orders.append({
                "dex": dex or "default",
                "coin": order.get("coin"),
                "side": order.get("side"),
                "sz": order.get("sz") or order.get("origSz"),
                "limitPx": order.get("limitPx"),
                "orderType": order.get("orderType"),
                "triggerPx": order.get("triggerPx"),
                "triggerCondition": order.get("triggerCondition"),
                "reduceOnly": order.get("reduceOnly"),
            })

    open_unrealized = sum(p["unrealizedPnl"] for p in positions)
    replay = analyze_replay(fills, open_unrealized)
    account_values["total_visible"] = (
        account_values["perp_default"] + account_values["perp_xyz"] + account_values["spot_total"]
    )
    return {
        "label": label,
        "address": main,
        "role": role,
        "fills": len(fills),
        "volume": volume,
        "closed_before_fees": closed,
        "fees": fees,
        "closed_after_fees": closed - fees,
        "open_unrealized": open_unrealized,
        "net_marked_now": closed - fees + open_unrealized,
        "account_values": account_values,
        "margin_summaries": margin_summaries,
        "spot_balances": spot_balances,
        "replay": replay,
        "by_coin": dict(sorted(by_coin.items(), key=lambda item: abs(item[1]["closed"] - item[1]["fees"]), reverse=True)),
        "positions": sorted(positions, key=lambda p: abs(p["positionValue"]), reverse=True),
        "orders": orders,
    }


def main():
    now = dt.datetime.now(CT)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.isoformat(),
        "window_ct": f"{start.strftime('%Y-%m-%d %I:%M %p %Z')} → {now.strftime('%I:%M %p %Z')}",
        "manual": fetch_account("Tim manual HL", MANUAL, start_ms, end_ms),
        "managed": fetch_account("Managed HL", load_managed_address(), start_ms, end_ms),
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"Manual net marked: ${payload['manual']['net_marked_now']:,.2f}")
    print(f"Managed net marked: ${payload['managed']['net_marked_now']:,.2f}")


if __name__ == "__main__":
    main()
