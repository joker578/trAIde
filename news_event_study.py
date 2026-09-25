"""
News event study: measure THIS bot's win rate BEFORE vs AFTER news releases.

Usage:
    .venv/bin/python news_event_study.py                 # all event kinds
    .venv/bin/python news_event_study.py --kinds PPI     # PPI only
    .venv/bin/python news_event_study.py --kinds PPI,CPI --window 60
    .venv/bin/python news_event_study.py --demo          # plumbing test (synthetic prices)

Data:
    Real mode needs data/btcusdt_1m.csv covering the event dates
    (run: .venv/bin/python fetch_data.py 730).
    If the CSV is missing or does not cover events, use --demo to verify
    the pipeline — demo win rates are MEANINGLESS (fake prices).

What it does:
    For each calendar event, runs the EMA strategy over [T-12h, T+12h],
    collects completed trades, and buckets them by entry time:
      PRE     = entered within [T-window, T)     (right before the release)
      POST    = entered within [T, T+window]     (right after the release)
      CONTEXT = other trades during the event day window
      CONTROL = same-duration windows on quiet days (no events nearby)
    Then prints win rate / avg return per bucket so you can compare
    "news" vs "normal" behaviour with real numbers.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.backtest.models import OneTickSlippageFillModel
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from strategies.ema_cross import EMACross
from strategies.ema_cross import EMACrossConfig

CALENDAR_PATH = Path("config/news_calendar.csv")
DATA_PATH = Path("data/btcusdt_1m.csv")
STARTING_CASH = 10_000
TRADE_SIZE = Decimal("0.01")
CONTEXT_HOURS = 12  # backtest window around each event
CONTROL_OFFSET_DAYS = 7  # quiet-day control windows


# --------------------------------------------------------------------- utils
def load_calendar(kinds: set[str]) -> list[dict]:
    events = []
    with CALENDAR_PATH.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["kind"] not in kinds:
                continue
            events.append(
                {
                    "ts": datetime.fromisoformat(row["timestamp"]),
                    "name": row["name"],
                    "kind": row["kind"],
                }
            )
    events.sort(key=lambda e: e["ts"])
    return events


def make_synthetic_span(start: datetime, end: datetime, seed: int = 42) -> pd.DataFrame:
    n = int((end - start).total_seconds() // 60)
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    # Fixed-period cycle (period ~30h) so the cumulative drift stays BOUNDED
    # no matter how many bars we generate (a slow sine would explode).
    drift = np.sin(2.0 * np.pi * i / 1800.0) * 0.00025
    noise = rng.normal(0.0, 0.0006, n)
    log_price = np.log(50_000.0) + np.cumsum(drift + noise)
    close = np.exp(np.clip(log_price, np.log(1_000.0), np.log(500_000.0)))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0.0, 0.0004, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(5.0, 50.0, n),
        },
        index=pd.date_range(start, periods=n, freq="1min", tz="UTC"),
    )


def run_window(window_df: pd.DataFrame, tag: str) -> list[dict]:
    """Run the EMA strategy over a bar window; return completed trades."""
    instrument = TestInstrumentProvider.btcusdt_binance()
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(f"EVENT-{tag}"[:20]),
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(STARTING_CASH, USDT)],
        base_currency=USDT,
        fee_model=MakerTakerFeeModel(),  # 0.1%/side — same honest costs as backtest
        fill_model=OneTickSlippageFillModel(),
    )
    engine.add_instrument(instrument)
    bars = BarDataWrangler(bar_type, instrument).process(window_df)
    engine.add_data(bars)

    strategy = EMACross(
        EMACrossConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            trade_size=TRADE_SIZE,
            allow_short=True,
            # Note: guardrails stay ON, but the news calendar is intentionally
            # NOT passed here — we want to observe how the naive bot behaves
            # around news (that's the measurement).
            news_calendar_path="",
        )
    )
    engine.add_strategy(strategy)
    engine.run()
    trades = [
        {**t, "entry_dt": datetime.fromtimestamp(t["entry_ts"] / 1e9, tz=timezone.utc)}
        for t in strategy.closed_trades
    ]
    engine.dispose()
    return trades


def bucket_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "wins": 0, "win_rate": None, "avg_ret": None, "sum_ret": 0.0}
    wins = sum(1 for t in trades if t["return_pct"] > 0)
    rets = [t["return_pct"] for t in trades]
    return {
        "n": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades),
        "avg_ret": float(np.mean(rets)),
        "sum_ret": float(np.sum(rets)),
    }


def fmt(stats: dict) -> str:
    if stats["n"] == 0:
        return f"{stats['n']:>5}  {'—':>5}  {'—':>8}  {'—':>9}  {'—':>9}"
    return (
        f"{stats['n']:>5}  {stats['wins']:>5}  "
        f"{stats['win_rate']:>7.1%}  {stats['avg_ret']:>+9.4%}  {stats['sum_ret']:>+9.4%}"
    )


# ---------------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser(description="News event study for trAIde bot")
    parser.add_argument(
        "--kinds",
        default="PPI,CPI,NFP,FOMC",
        help="Comma list of event kinds (default: PPI,CPI,NFP,FOMC)",
    )
    parser.add_argument(
        "--window", type=int, default=60, help="Minutes before/after each event"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Force synthetic prices (pipeline test only — numbers are meaningless)",
    )
    args = parser.parse_args()
    kinds = {k.strip().upper() for k in args.kinds.split(",") if k.strip()}
    # Normalize NFP alias
    kinds = {"NFP" if k == "NFP" else k for k in kinds}

    all_events = load_calendar(kinds)
    if not all_events:
        sys.exit(f"No events found in {CALENDAR_PATH} for kinds={sorted(kinds)}")

    demo = args.demo
    price_df: pd.DataFrame | None = None
    if DATA_PATH.exists() and not args.demo:
        price_df = pd.read_csv(DATA_PATH, parse_dates=["timestamp"], index_col="timestamp")
        if price_df.index.tz is None:
            price_df.index = price_df.index.tz_localize("UTC")
        price_df = price_df[["open", "high", "low", "close", "volume"]].astype(float)

    # Filter to events with data coverage (or all, for demo)
    if price_df is not None:
        lo, hi = price_df.index.min(), price_df.index.max()
        events = [e for e in all_events if lo <= e["ts"] <= hi]
        if not events:
            sys.exit(
                f"Events found: {len(all_events)}, but NONE fall inside your data range "
                f"({lo} .. {hi}).\nRun: .venv/bin/python fetch_data.py 730  "
                "or use --demo."
            )
        skipped = len(all_events) - len(events)
        print(f"Data: {DATA_PATH} ({len(price_df):,} bars, {lo} .. {hi})")
        print(f"Events with coverage: {len(events)} (skipped {skipped} outside range)")
    else:
        demo = True
        events = all_events
        lo = events[0]["ts"] - timedelta(days=2)
        hi = events[-1]["ts"] + timedelta(days=2)
        price_df = make_synthetic_span(lo, hi)
        print(f"DEMO MODE: {len(events)} real event times, SYNTHETIC prices.")
        print("Win rates below are PLUMBING-ONLY numbers — NOT market evidence.\n")

    window = timedelta(minutes=args.window)
    context = timedelta(hours=CONTEXT_HOURS)

    buckets: dict[str, list[dict]] = {
        "PRE": [],
        "POST": [],
        "CONTEXT": [],
        "CONTROL": [],
    }
    used_control_starts: set[datetime] = set()

    for ev in events:
        t = ev["ts"]
        w_start, w_end = t - context, t + context
        if w_start < price_df.index.min() or w_end > price_df.index.max():
            continue
        window_df = price_df.loc[w_start:w_end]
        trades = run_window(window_df, tag=ev["kind"])
        for tr in trades:
            dt = tr["entry_dt"]
            if t - window <= dt < t:
                buckets["PRE"].append(tr)
            elif t <= dt <= t + window:
                buckets["POST"].append(tr)
            else:
                buckets["CONTEXT"].append(tr)

        # Quiet-day control: same clock-time windows, +/- 7 days, no event nearby
        for offset in (-CONTROL_OFFSET_DAYS, CONTROL_OFFSET_DAYS):
            c_t = t + timedelta(days=offset)
            if any(
                abs((c_t - e["ts"]).total_seconds()) < 180 * 60 for e in all_events
            ):
                continue  # another event too close — not quiet
            c_start = c_t - context
            if c_start in used_control_starts:
                continue
            if c_start < price_df.index.min() or c_t + context > price_df.index.max():
                continue
            used_control_starts.add(c_start)
            c_df = price_df.loc[c_start : c_t + context]
            c_trades = run_window(c_df, tag="CTRL")
            for tr in c_trades:
                dt = tr["entry_dt"]
                if c_t - window <= dt <= c_t + window:
                    buckets["CONTROL"].append(tr)

    # ------------------------------------------------------------------ report
    mode = "DEMO (synthetic prices — plumbing only)" if demo else "REAL market data"
    print(f"\n{'=' * 74}")
    print(
        f" EVENT STUDY [{mode}]  kinds={','.join(sorted(kinds))}  "
        f"window=+/-{args.window}m  events_used≈{len(events)}"
    )
    print("=" * 74)
    print(f"{'Bucket':<10} {'Trades':>5}  {'Wins':>5}  {'WinRate':>8}  "
          f"{'AvgRet':>9}  {'SumRet':>9}")
    print("-" * 74)
    for name in ("PRE", "POST", "CONTEXT", "CONTROL"):
        print(f"{name:<10} {fmt(bucket_stats(buckets[name]))}")
    print("-" * 74)

    pre, post, ctrl = (bucket_stats(buckets[k]) for k in ("PRE", "POST", "CONTROL"))
    print(
        """
HOW TO READ THIS:
- POST vs CONTROL is the key comparison: does the bot do better or worse
  in the hour AFTER a release than on quiet days?
- PRE shows whether it was already getting chopped BEFORE the print.
- Small sample sizes (n<30) are noise — do not trust a 50% swing on 6 trades.
- Returns are price moves only (fees/slippage not subtracted).
- The LIVE strategy uses a +/-30m news blackout, so these numbers describe
  the NAIVE bot (no blackout) — they are the reason the blackout exists.
"""
    )


if __name__ == "__main__":
    main()
