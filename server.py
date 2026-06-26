#!/usr/bin/env python3
"""Local dashboard server with live Hyperliquid refresh endpoint.

Serves index.html/static files and exposes:
- GET /api/latest   -> current data/latest.json
- POST /api/refresh -> run scripts/fetch_hl_pnl.py then return latest JSON

Read-only: refresh script uses Hyperliquid Info API only.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "latest.json"
FETCH = ROOT / "scripts" / "fetch_hl_pnl.py"
MAX_LATEST_AGE_SECONDS = 30


def run_refresh(timeout: int = 75):
    return subprocess.run(
        ["python3", str(FETCH)],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def latest_age_seconds(payload: dict) -> float | None:
    generated = payload.get("generated_at")
    if not generated:
        return None
    try:
        generated_dt = dt.datetime.fromisoformat(str(generated))
        if generated_dt.tzinfo is None:
            generated_dt = generated_dt.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(generated_dt.tzinfo) - generated_dt).total_seconds()
    except Exception:
        return None


class Handler(SimpleHTTPRequestHandler):
    server_version = "TradingDashboard/0.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path == "/api/latest":
            if not DATA.exists():
                proc = run_refresh()
                if proc.returncode != 0:
                    self._json(500, {"ok": False, "error": proc.stderr or proc.stdout, "returncode": proc.returncode})
                    return
            try:
                payload = json.loads(DATA.read_text(encoding="utf-8"))
                age = latest_age_seconds(payload)
                if age is None or age > MAX_LATEST_AGE_SECONDS:
                    proc = run_refresh()
                    if proc.returncode == 0:
                        payload = json.loads(DATA.read_text(encoding="utf-8"))
                        payload["refresh_stdout"] = proc.stdout.strip()
                    else:
                        payload["stale_warning"] = proc.stderr or proc.stdout
                self._json(200, payload)
            except subprocess.TimeoutExpired:
                self._json(504, {"ok": False, "error": "Hyperliquid refresh timed out"})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path != "/api/refresh":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            proc = run_refresh()
            if proc.returncode != 0:
                self._json(500, {"ok": False, "error": proc.stderr or proc.stdout, "returncode": proc.returncode})
                return
            payload = json.loads(DATA.read_text(encoding="utf-8"))
            payload["refresh_stdout"] = proc.stdout.strip()
            self._json(200, payload)
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "Hyperliquid refresh timed out"})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving trading dashboard on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
