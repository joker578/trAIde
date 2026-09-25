"""
Download Binance BTCUSDT perpetual funding-rate history (public API, NO key).

Usage (run on YOUR machine — sandbox networks are blocked):
    .venv/bin/python fetch_funding.py            # full available history
    .venv/bin/python fetch_funding.py 365        # last 365 days

Output: data/funding_btcusdt.csv  (timestamp, rate, annualized_pct)
Then:   .venv/bin/python carry_backtest.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
OUT_DIR = Path("data")
OUT_PATH = OUT_DIR / "funding_btcusdt.csv"
SYMBOL = "BTCUSDT"


def get_json(url: str, params: dict) -> list | dict:
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{qs}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def detect_interval_ms() -> int:
    """Funding interval from premiumIndex (8h default; Binance varies per coin)."""
    try:
        data = get_json(PREMIUM_URL, {"symbol": SYMBOL})
        interval = data.get("fundingInterval")
        if interval:
            return int(interval)
    except Exception:
        pass
    return 8 * 60 * 60 * 1000  # 8h


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365 * 5
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )
    interval_ms = detect_interval_ms()
    periods_per_year = (365 * 24 * 3600 * 1000) / interval_ms

    print(f"Fetching {SYMBOL} funding rates, last {days} day(s)...")
    print(f"Detected funding interval: {interval_ms / 3600000:.1f}h "
          f"({periods_per_year:.0f} periods/year)")

    rows: list[dict] = []
    cursor = start_ms
    while cursor < now_ms:
        batch = get_json(
            FUNDING_URL,
            {
                "symbol": SYMBOL,
                "startTime": cursor,
                "endTime": now_ms,
                "limit": 1000,
            },
        )
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            rate = float(item["fundingRate"])
            rows.append(
                {
                    "timestamp": datetime.fromtimestamp(
                        int(item["fundingTime"]) / 1000, tz=timezone.utc
                    ).isoformat(),
                    "rate": rate,
                    "annualized_pct": f"{rate * periods_per_year * 100:.4f}",
                }
            )
        cursor = int(batch[-1]["fundingTime"]) + interval_ms
        print(f"  ... {len(rows):,} periods", end="\r")
        if len(batch) < 1000:
            break
        time.sleep(0.2)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "rate", "annualized_pct"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows):,} funding periods -> {OUT_PATH}")
    print("Now run: .venv/bin/python carry_backtest.py")


if __name__ == "__main__":
    main()
