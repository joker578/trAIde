"""
Download real BTCUSDT 1-minute klines from Binance's public API (no key needed)
and save them to data/btcusdt_1m.csv for the backtester.

Usage:
    .venv/bin/python fetch_data.py            # ~5 days of 1m candles
    .venv/bin/python fetch_data.py 30         # 30 days of 1m candles

Notes:
- Public market data only — no account, no API key, no orders.
- If your network blocks Binance, run this on a normal machine/connection
  and copy the CSV into data/.
"""

from __future__ import annotations

import csv
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://api.binance.com/api/v3/klines"
OUT_DIR = Path("data")
OUT_PATH = OUT_DIR / "btcusdt_1m.csv"
LIMIT = 1_000  # max per request
DAYS_DEFAULT = 5


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": LIMIT,
        }
    )
    url = f"{BASE}?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        # Minimal JSON parse without extra deps
        import json

        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS_DEFAULT
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[list] = []
    cursor = start_ms

    print(f"Fetching BTCUSDT 1m klines for the last {days} day(s)...")
    while cursor < end_ms:
        try:
            batch = fetch_klines("BTCUSDT", "1m", cursor, end_ms)
        except urllib.error.URLError as exc:
            print(
                f"\nNetwork error reaching Binance: {exc}\n"
                "Your network may block the API. Workaround: run this on another "
                "machine/connection, then copy the CSV into data/."
            )
            sys.exit(1)
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        cursor = last_open + 60_000
        print(f"  ... {len(rows):,} candles", end="\r")
        if len(batch) < LIMIT:
            break
        time.sleep(0.15)  # be polite to the public API

    with OUT_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for k in rows:
            ts = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).isoformat()
            writer.writerow([ts, k[1], k[2], k[3], k[4], k[5]])

    print(f"\nSaved {len(rows):,} candles -> {OUT_PATH}")
    print("Now run: .venv/bin/python run_backtest.py")


if __name__ == "__main__":
    main()
