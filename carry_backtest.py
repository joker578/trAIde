"""
Funding-rate carry backtest: the market-neutral "boring" quant engine.

How it works (10-yr-old version):
  When the perp price runs above spot, LONGS pay SHORTS funding every 8h.
  Carry trade: buy BTC spot + short BTC perp (price-neutral) -> collect funding.
  You don't care if BTC goes up or down.

Usage:
    .venv/bin/python carry_backtest.py              # real data (fetch_funding.py first)
    .venv/bin/python carry_backtest.py --demo       # synthetic 3y funding history

Model (stated honestly, no black boxes):
  + PnL  = sum(funding rates while holding) * notional
  - Fees = 0.4% of notional per hold episode (open spot+perp, close spot+perp)
  - Threshold strategy never peeks: hold period t+1 only if period t's
    annualized funding > threshold (no look-ahead).
  - EXCLUDED (real risks you must judge): basis/price moves on entry/exit,
    exchange risk, funding interval changes, missed settlements.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

FUNDING_CSV = Path("data/funding_btcusdt.csv")
ROUND_TRIP_FEE = 0.004  # 4 taker fills * 0.1% (spot in/out + perp in/out)
THRESHOLDS = [0.0, 0.05, 0.10, 0.20]  # annualized: 0%, 5%, 10%, 20%


def load_demo(periods: int = 3 * 365 * 3) -> tuple[list[float], list[str]]:
    """Synthetic 3-year funding: bull regimes pay, bear regimes charge, chop ~0."""
    rng = np.random.default_rng(7)
    regimes = []
    # 3y of 8h periods, cycle through regimes of random length
    while len(regimes) < periods:
        kind = rng.choice(["bull", "bear", "chop"], p=[0.4, 0.3, 0.3])
        length = int(rng.integers(60, 400))
        if kind == "bull":
            base = rng.uniform(0.0002, 0.0012)  # per period
        elif kind == "bear":
            base = -rng.uniform(0.0001, 0.0008)
        else:
            base = rng.normal(0.0, 0.0001)
        regimes.extend([base + rng.normal(0, 0.0001) for _ in range(length)])
    rates = regimes[:periods]
    timestamps = [f"synthetic-{i}" for i in range(periods)]
    return rates, timestamps


def load_csv() -> tuple[list[float], list[str]]:
    if not FUNDING_CSV.exists():
        sys.exit(
            f"{FUNDING_CSV} not found.\n"
            "Run first: .venv/bin/python fetch_funding.py   (needs internet)\n"
            "Or demo:    .venv/bin/python carry_backtest.py --demo"
        )
    rates, stamps = [], []
    with FUNDING_CSV.open() as f:
        for row in csv.DictReader(f):
            rates.append(float(row["rate"]))
            stamps.append(row["timestamp"])
    return rates, stamps


def annualize(rate_per_period: float, periods_per_year: float) -> float:
    return rate_per_period * periods_per_year


def run_strategy(
    rates: list[float], periods_per_year: float, threshold_ann: float | None
) -> dict:
    """threshold None = always on. Returns net stats (fees included)."""
    hold: list[bool] = []
    for i in range(len(rates)):
        if threshold_ann is None:
            hold.append(True)
        elif i == 0:
            hold.append(False)  # no prior info yet
        else:
            # signal from PREVIOUS period (no look-ahead)
            prev_ann = annualize(rates[i - 1], periods_per_year)
            hold.append(prev_ann > threshold_ann)

    # episodes = contiguous hold blocks (each pays ROUND_TRIP_FEE once)
    episodes, in_pos = 0, False
    gross_periods = 0
    for h in hold:
        if h and not in_pos:
            episodes += 1
            in_pos = True
        elif not h:
            in_pos = False
        if h:
            gross_periods += 1

    held_rates = [r for r, h in zip(rates, hold) if h]
    gross = sum(held_rates)  # per-unit notional
    fees = episodes * ROUND_TRIP_FEE
    net = gross - fees

    n = len(held_rates) if held_rates else 1
    years = len(rates) / periods_per_year
    ann_gross = (1 + gross) ** (1 / years) - 1 if years > 0 else 0.0
    ann_net = (1 + net) ** (1 / years) - 1 if years > 0 else 0.0

    # funding drought: longest run of consecutive NEGATIVE held periods
    longest_drought = run = 0
    for r in held_rates:
        run = run + 1 if r < 0 else 0
        longest_drought = max(longest_drought, run)

    # funding-only equity curve of held periods (for max funding drawdown)
    curve = np.cumsum(held_rates) if held_rates else np.array([0.0])
    peak = np.maximum.accumulate(curve)
    max_dd = float(np.max(peak - curve)) if len(curve) else 0.0

    pos = sum(1 for r in held_rates if r > 0)
    return {
        "time_in_market": gross_periods / len(rates) if rates else 0.0,
        "episodes": episodes,
        "ann_gross": ann_gross,
        "ann_net": ann_net,
        "longest_drought": longest_drought,
        "max_funding_dd": max_dd,  # in % of notional (sum of rates)
        "period_win_rate": pos / n if held_rates else 0.0,
        "total_net": net,
    }


def fmt_pct(x: float) -> str:
    return f"{x:+.2%}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Funding carry backtest")
    parser.add_argument("--demo", action="store_true", help="synthetic funding history")
    parser.add_argument(
        "--capital", type=float, default=10_000, help="notional per unit"
    )
    args = parser.parse_args()

    if args.demo:
        rates, stamps = load_demo()
        print(
            f"DEMO MODE: {len(rates):,} SYNTHETIC 8h funding periods (3 years).\n"
            "Process check only — not market evidence.\n"
        )
        periods_per_year = 3 * 365
    else:
        rates, stamps = load_csv()
        print(f"Loaded {len(rates):,} funding periods from {FUNDING_CSV}")
        periods_per_year = 3 * 365  # 8h default
        try:
            from datetime import datetime

            t0 = datetime.fromisoformat(stamps[0])
            t1 = datetime.fromisoformat(stamps[-1])
            years = (t1 - t0).total_seconds() / (365 * 24 * 3600)
            periods_per_year = len(rates) / max(years, 1e-9)
        except Exception:
            pass

    print(
        f"Periods/year ≈ {periods_per_year:.0f}  |  round-trip cost "
        f"{ROUND_TRIP_FEE:.2%} of notional  |  capital ${args.capital:,.0f}\n"
    )

    print(
        f"{'Strategy':<24} {'InMkt':>6} {'Episodes':>8} "
        f"{'GrossAnn':>9} {'NET Ann':>9} {'WinPd':>6} {'Drought':>8}"
    )
    print("-" * 78)

    always = run_strategy(rates, periods_per_year, None)
    print(
        f"{'Always on':<24} {always['time_in_market']:>5.0%} {always['episodes']:>8} "
        f"{always['ann_gross']:>9.2%} {fmt_pct(always['ann_net']):>9} "
        f"{always['period_win_rate']:>6.0%} {always['longest_drought']:>5} pd"
    )
    for thr in THRESHOLDS:
        s = run_strategy(rates, periods_per_year, thr)
        print(
            f"{f'Threshold >{thr:.0%} ann.':<24} {s['time_in_market']:>5.0%} "
            f"{s['episodes']:>8} {s['ann_gross']:>9.2%} {fmt_pct(s['ann_net']):>9} "
            f"{s['period_win_rate']:>6.0%} {s['longest_drought']:>5} pd"
        )

    fees_paid = always["episodes"] * ROUND_TRIP_FEE
    print(
        f"""
NOTES:
- Fees shown: always-on pays {fees_paid:.2%} total ({always['episodes']} episodes).
  Threshold mode pays per re-entry — churn is the enemy of carry.
- 'NET Ann' is what a unit of notional earns ANNUALLY after fees, funding only.
- Longest drought = consecutive periods where you PAID funding (negative carry).
- EXCLUDED risks: basis moves at entry/exit, exchange failure, interval changes.
- {'DEMO = synthetic regimes, process check only.' if args.demo else 'Real data: funding only — treat as lower bound of a live carry book.'}
"""
    )


if __name__ == "__main__":
    main()
