"""
Live funding-carry MONITOR — tells you when to open/close the carry trade.

Runs on public data only — NO API keys needed for the monitor itself.

Usage:
    .venv/bin/python run_carry.py --demo             # fake print, offline
    .venv/bin/python run_carry.py --once             # one real check (needs internet)
    .venv/bin/python run_carry.py --interval 60      # keep watching (60s poll)
    .venv/bin/python run_carry.py --once --capital 50 --threshold 0.10

When verdict is OPEN, the trade (on YOUR Binance account, done by you) is:
    1. Buy $C BTC spot
    2. Short $C BTCUSDT perp (isolated margin recommended, SMALL leverage)
    3. Collect funding every interval while signal stays hot
    4. Close both legs when verdict flips to CLOSE/STAND ASIDE

This tool does NOT place orders. Auto-execution comes after testnet validation.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
SYMBOL = "BTCUSDT"


def fetch_premium() -> dict:
    qs = urllib.parse.urlencode({"symbol": SYMBOL})
    with urllib.request.urlopen(f"{PREMIUM_URL}?{qs}", timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def analyze(data: dict, threshold: float) -> dict:
    rate = float(data.get("lastFundingRate", 0.0))
    interval_ms = int(data.get("fundingInterval", 8 * 60 * 60 * 1000))
    periods_per_year = (365 * 24 * 3600 * 1000) / interval_ms
    ann = rate * periods_per_year
    mark = float(data.get("markPrice", 0.0))
    index = float(data.get("indexPrice", 0.0))
    basis = (mark - index) / index if index else 0.0
    nxt = data.get("nextFundingTime")
    next_dt = (
        datetime.fromtimestamp(int(nxt) / 1000, tz=timezone.utc) if nxt else None
    )
    verdict = "OPEN CARRY" if ann > threshold else "STAND ASIDE"
    if ann < 0:
        verdict = "SHORTS PAY / STAND ASIDE"
    return {
        "rate": rate,
        "interval_h": interval_ms / 3_600_000,
        "ann": ann,
        "basis": basis,
        "next": next_dt,
        "verdict": verdict,
        "open": ann > threshold,
    }


def print_report(a: dict, capital: float) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"\n[{now}] {SYMBOL} funding check")
    print(f"  funding rate:   {a['rate']:+.4%} per {a['interval_h']:.1f}h")
    print(f"  annualized:     {a['ann']:+.2%}")
    print(f"  perp-spot basis:{a['basis']:+.4%}")
    if a["next"]:
        print(f"  next funding:   {a['next']:%Y-%m-%d %H:%M}Z")
    print(f"  VERDICT:        {a['verdict']}")
    if a["open"]:
        print(
            f"""
  To open (manual, on your account):
    - Buy  ${capital:,.2f} BTC spot
    - Short ${capital:,.2f} BTCUSDT perp (isolated margin, 1-2x ONLY)
    - Every {a['interval_h']:.0f}h while verdict stays OPEN you RECEIVE
      ~${capital * abs(a['rate']):,.4f} per settlement (sign flips if rate flips)
    - Close BOTH legs when verdict flips away from OPEN
  Warnings: funding can go negative; perp short gets liquidated if BTC pumps
  hard (use isolated + wide margin); basis at entry/exit is real cost.""" 
        )


def demo_data() -> dict:
    return {
        "lastFundingRate": "0.000125",  # 0.0125% / period
        "fundingInterval": 8 * 60 * 60 * 1000,
        "markPrice": "50012.50",
        "indexPrice": "50000.00",
        "nextFundingTime": str(
            int(time.time() * 1000) + 4 * 60 * 60 * 1000
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Funding carry monitor")
    parser.add_argument("--demo", action="store_true", help="fake data, offline")
    parser.add_argument("--once", action="store_true", help="single check then exit")
    parser.add_argument("--interval", type=int, default=60, help="poll seconds")
    parser.add_argument("--capital", type=float, default=50.0)
    parser.add_argument(
        "--threshold", type=float, default=0.10, help="annualized funding to OPEN"
    )
    args = parser.parse_args()

    if args.demo:
        print("DEMO MODE — synthetic funding print, no network used.")
        a = analyze(demo_data(), args.threshold)
        print_report(a, args.capital)
        return

    if args.once:
        a = analyze(fetch_premium(), args.threshold)
        print_report(a, args.capital)
        return

    print(
        f"Watching {SYMBOL} funding every {args.interval}s "
        f"(threshold {args.threshold:.0%} annualized). Ctrl+C to stop."
    )
    try:
        while True:
            try:
                a = analyze(fetch_premium(), args.threshold)
                print_report(a, args.capital)
            except Exception as exc:
                print(f"  fetch error: {exc}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
