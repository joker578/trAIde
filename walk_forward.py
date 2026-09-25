"""
Walk-forward test: the ONLY backtest result that isn't allowed to lie.

Idea:
  Split history into rolling (train -> test) windows.
  On TRAIN: try every parameter combo, pick the best by expectancy.
  On TEST: run ONLY that winner on data it has never seen.
  Repeat, roll forward. The aggregated TEST numbers are out-of-sample (OOS) —
  the closest thing to "what would have happened live".

Usage:
    .venv/bin/python walk_forward.py                 # synthetic demo (offline)
    .venv/bin/python walk_forward.py --data data/btcusdt_1m.csv --timeframe 1h
    .venv/bin/python walk_forward.py --train-days 60 --test-days 15
    .venv/bin/python walk_forward.py --max-windows 8

Metric: EXPECTANCY per trade, net of 0.1%/side fees + 1-tick slippage.
Win rate is reported but never optimized.
"""

from __future__ import annotations

import argparse
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

STARTING_CASH = 10_000
TRADE_SIZE = Decimal("0.01")
MIN_TRAIN_TRADES = 8  # refuse to pick a "best" combo with fewer trades than this
MIN_TEST_TRADES = 2   # a test window with <2 trades can't judge expectancy

# --- parameter grid (small on purpose: fewer combos = less curve-fitting) ---
GRID_FAST = [5, 10, 15, 20]
GRID_SLOW = [30, 50, 100]
GRID_FILTER = [True, False]  # 200-EMA trend filter on/off

# Risk params are FIXED during search — only signal params are optimized,
# otherwise the optimizer overfits risk settings too.
RISK = dict(
    stop_loss_pct=0.02,
    trailing_stop_pct=0.015,
    daily_loss_limit_pct=0.03,
    risk_per_trade_pct=0.01,
    max_notional_frac=0.5,
    allow_short=True,
)


def grid_combos() -> list[dict]:
    return [
        {"fast": f, "slow": s, "filter": t}
        for t in GRID_FILTER
        for f in GRID_FAST
        for s in GRID_SLOW
        if f < s
    ]


def run_one(
    df: pd.DataFrame, params: dict, bar_type: BarType, tag: str
) -> dict:
    """Backtest one parameter combo on one window; return net stats."""
    instrument = TestInstrumentProvider.btcusdt_binance()
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(f"WF-{tag}"[:20]),
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(STARTING_CASH, USDT)],
        base_currency=USDT,
        fee_model=MakerTakerFeeModel(),
        fill_model=OneTickSlippageFillModel(),
    )
    engine.add_instrument(instrument)
    engine.add_data(BarDataWrangler(bar_type, instrument).process(df))

    strategy = EMACross(
        EMACrossConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            trade_size=TRADE_SIZE,
            fast_ema_period=params["fast"],
            slow_ema_period=params["slow"],
            use_trend_filter=params["filter"],
            news_calendar_path="",  # pure signal test
            **RISK,
        )
    )
    engine.add_strategy(strategy)
    engine.run()

    nets = [t["return_pct"] for t in strategy.closed_trades]
    wins = [r for r in nets if r > 0]
    stats = {
        "n": len(nets),
        "expectancy": float(np.mean(nets)) if nets else None,
        "win_rate": (len(wins) / len(nets)) if nets else None,
        "gross": float(sum(nets)) if nets else 0.0,
    }
    engine.dispose()
    return stats


def make_synthetic_1h(days: int, seed: int = 42) -> pd.DataFrame:
    n = days * 24
    rng = np.random.default_rng(seed)
    i = np.arange(n)
    # Multiple trend regimes at hourly scale (bounded cycles + noise)
    drift = (
        np.sin(2 * np.pi * i / 360.0) * 0.0015
        + np.sin(2 * np.pi * i / 120.0) * 0.0007
    )
    log_p = np.log(50_000.0) + np.cumsum(drift + rng.normal(0.0, 0.004, n))
    close = np.exp(np.clip(log_p, np.log(1_000.0), np.log(500_000.0)))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0.0, 0.002, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(10.0, 200.0, n),
        },
        index=pd.date_range(
            datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=days),
            periods=n,
            freq="1h",
            tz="UTC",
        ),
    )


def load_data(path: str | None, timeframe: str, synth_days: int):
    """Returns (df_1h, is_demo, source_desc)."""
    if path:
        p = Path(path)
        if not p.exists():
            sys.exit(f"Data file not found: {p}")
        df = pd.read_csv(p, parse_dates=["timestamp"], index_col="timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        if timeframe != "1m":
            rule = {"1h": "1h", "4h": "4h"}.get(timeframe)
            if rule is None:
                sys.exit(f"Unsupported timeframe: {timeframe}")
            df = (
                df.resample(rule)
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna()
            )
        return df, False, f"{p} @ {timeframe} ({len(df):,} bars)"
    df = make_synthetic_1h(synth_days)
    if timeframe != "1h":
        sys.exit("--timeframe with synthetic data only supports 1h")
    return df, True, f"SYNTHETIC {synth_days}d @ 1h ({len(df):,} bars)"


def pick_best(train_df: pd.DataFrame, bar_type: BarType) -> tuple[dict | None, dict]:
    best_params, best_stats, best_score = None, None, -np.inf
    for params in grid_combos():
        st = run_one(train_df, params, bar_type, tag="TR")
        if st["n"] < MIN_TRAIN_TRADES or st["expectancy"] is None:
            continue
        if st["expectancy"] > best_score:
            best_score, best_params, best_stats = st["expectancy"], params, st
    return best_params, best_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward test (honest OOS)")
    parser.add_argument("--data", default=None, help="CSV path (e.g. data/btcusdt_1m.csv)")
    parser.add_argument("--timeframe", default="1h", choices=["1m", "1h", "4h"])
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=15)
    parser.add_argument("--max-windows", type=int, default=0, help="0 = all")
    parser.add_argument("--synth-days", type=int, default=365, help="synthetic span")
    args = parser.parse_args()

    df, demo, source = load_data(args.data, args.timeframe, args.synth_days)
    tf = args.timeframe
    tf_map = {"1m": "1-MINUTE", "1h": "1-HOUR", "4h": "4-HOUR"}
    bar_type = BarType.from_str(
        f"BTCUSDT.BINANCE-{tf_map[tf]}-LAST-EXTERNAL"
    )
    print(f"Data: {source}")
    if demo:
        print(
            "DEMO MODE: synthetic prices — verifies the PROCESS. "
            "OOS numbers are not market evidence.\n"
        )

    combos = grid_combos()
    print(
        f"Grid: {len(combos)} combos "
        f"(fast={GRID_FAST}, slow={GRID_SLOW}, filter={GRID_FILTER}) "
        f"| train={args.train_days}d  test={args.test_days}d  "
        f"| metric=expectancy (net of fees)\n"
    )

    train_bars = args.train_days * (24 if tf in ("1h", "4h") else 1440 if tf == "1m" else 24)
    test_bars = args.test_days * (24 if tf in ("1h", "4h") else 1440 if tf == "1m" else 24)
    if tf == "4h":
        train_bars = args.train_days * 6
        test_bars = args.test_days * 6

    n = len(df)
    if n < train_bars + test_bars:
        sys.exit(f"Not enough data: {n} bars < train {train_bars} + test {test_bars}")

    # Rolling-origin windows
    windows = []
    t0 = 0
    while t0 + train_bars + test_bars <= n:
        windows.append((t0, t0 + train_bars, t0 + train_bars + test_bars))
        t0 += test_bars
        if args.max_windows and len(windows) >= args.max_windows:
            break

    # Baseline (static params) evaluated on every TEST window for reference
    static_params = {"fast": 10, "slow": 50, "filter": True}

    results = []
    for idx, (a, b, c) in enumerate(windows, 1):
        train_df, test_df = df.iloc[a:b], df.iloc[b:c]
        best_params, train_stats = pick_best(train_df, bar_type)
        if best_params is None:
            print(f"Window {idx}: no combo met min trades on train — skipped")
            continue
        test_stats = run_one(test_df, best_params, bar_type, tag=f"T{idx}")
        static_stats = run_one(test_df, static_params, bar_type, tag=f"S{idx}")
        results.append(
            {
                "i": idx,
                "train": f"{train_df.index[0]:%m-%d}..{train_df.index[-1]:%m-%d}",
                "test": f"{test_df.index[0]:%m-%d}..{test_df.index[-1]:%m-%d}",
                "params": best_params,
                "train_e": train_stats["expectancy"],
                "test_e": test_stats["expectancy"],
                "test_n": test_stats["n"],
                "test_wr": test_stats["win_rate"],
                "static_e": static_stats["expectancy"],
                "static_n": static_stats["n"],
            }
        )

    if not results:
        sys.exit("No valid windows — reduce --train-days/--test-days or add data.")

    # ---------------------------------------------------------------- report
    mode = "DEMO" if demo else "REAL"
    print(f"{'=' * 96}")
    print(f" WALK-FORWARD RESULTS [{mode}]  — train-select, test-validate")
    print(f"{'=' * 96}")
    header = (
        f"{'#':>2}  {'train':<13} {'test':<13} {'winner':<14} "
        f"{'trainE':>8} {'TEST E':>8} {'n':>4} {'WR':>6} {'staticE':>8}"
    )
    print(header)
    print("-" * 96)
    for r in results:
        p = r["params"]
        label = f"{p['fast']}/{p['slow']}{'+' if p['filter'] else '-'}TF"
        te = f"{r['test_e']:+.2%}" if r["test_e"] is not None else "n/a"
        se = f"{r['static_e']:+.2%}" if r["static_e"] is not None else "n/a"
        wr = f"{r['test_wr']:.0%}" if r["test_wr"] is not None else "—"
        print(
            f"{r['i']:>2}  {r['train']:<13} {r['test']:<13} {label:<14} "
            f"{r['train_e']:+8.2%} {te:>8} {r['test_n']:>4} {wr:>6} {se:>8}"
        )

    oos = [r["test_e"] for r in results if r["test_e"] is not None]
    oos_valid = [r for r in results if r["test_e"] is not None and r["test_n"] >= MIN_TEST_TRADES]
    static = [r["static_e"] for r in results if r["static_e"] is not None and r["static_n"] >= MIN_TEST_TRADES]
    print("-" * 96)
    if oos_valid:
        vals = [r["test_e"] for r in oos_valid]
        pos = sum(1 for v in vals if v > 0)
        print(
            f" OOS (optimized):  windows={len(vals)}  "
            f"mean expectancy={np.mean(vals):+.2%}  "
            f"median={np.median(vals):+.2%}  "
            f"positive={pos}/{len(vals)}  "
            f"trades={sum(r['test_n'] for r in oos_valid)}"
        )
    if static:
        print(
            f" OOS (static 10/50+TF baseline): mean={np.mean(static):+.2%}  "
            f"positive={sum(1 for v in static if v > 0)}/{len(static)}"
        )
    print(
        f"""
HOW TO READ THIS:
- Only the TEST columns are honest. Train columns are what the optimizer
  saw — they are ALWAYS flattering. Big train->test drop = overfitting.
- Optimized OOS vs static OOS: if optimization doesn't clearly beat the
  static baseline, the grid is just curve-fitting — use static params.
- Expectancy must be positive across MOST windows (not one lucky window)
  before you even think about testnet, let alone real money.
- {'DEMO numbers = process check only.' if demo else 'Fees: 0.1%/side + 1 tick slippage.'}
"""
    )


if __name__ == "__main__":
    main()
