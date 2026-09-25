"""
Backtest (paper money on historical data) — run this FIRST.

Usage:
    .venv/bin/python run_backtest.py

Data priority:
  1. data/btcusdt_1m.csv   — real candles from `python fetch_data.py`
  2. synthetic bars        — offline fallback so the demo always runs

No API keys. No real money. Ever.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine
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

N_BARS = 5_000  # ~3.5 days of 1-minute bars (synthetic fallback)
STARTING_CASH = 10_000  # fake USDT
TRADE_SIZE = Decimal("0.01")  # BTC per trade — keep it small
CSV_PATH = Path("data/btcusdt_1m.csv")

# Risk guardrails — tune carefully, these are your seatbelt
STOP_LOSS_PCT = 0.02  # exit if a trade loses 2%
TAKE_PROFIT_PCT = 0.04  # bank profit at +4% (2:1 reward:risk)
DAILY_LOSS_LIMIT_PCT = 0.03  # halt for the day if down 3% from day start

# News blackout — sit out PPI/CPI/NFP/FOMC chaos
NEWS_CALENDAR = Path("config/news_calendar.csv")
NEWS_BLACKOUT_MINUTES = 30


def make_synthetic_bars(n: int = N_BARS, seed: int = 42) -> pd.DataFrame:
    """Generate a realistic-looking random walk with trending regimes."""
    rng = np.random.default_rng(seed)
    drift = np.sin(np.linspace(0, 8 * np.pi, n)) * 0.00035
    noise = rng.normal(0.0, 0.0006, n)
    close = 50_000.0 * np.exp(np.cumsum(drift + noise))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0.0, 0.0004, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.uniform(5.0, 50.0, n)
    index = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def load_bars() -> pd.DataFrame:
    if CSV_PATH.exists():
        df = pd.read_csv(CSV_PATH, parse_dates=["timestamp"], index_col="timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        # Keep only the columns the wrangler needs, in order
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        print(f"Using REAL candles from {CSV_PATH} ({len(df):,} bars)")
        return df
    print("No CSV found — using SYNTHETIC candles (offline demo).")
    print("Tip: run `python fetch_data.py` for real Binance history.")
    return make_synthetic_bars()


def final_usdt_balance(engine: BacktestEngine) -> float | None:
    try:
        report = engine.trader.generate_account_report(Venue("BINANCE"))
        usdt = report[report["currency"] == "USDT"]
        if usdt.empty:
            return None
        return float(usdt["total"].iloc[-1])
    except Exception:
        return None


def main() -> None:
    instrument = TestInstrumentProvider.btcusdt_binance()
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(log_level="WARNING"),
        )
    )

    # MARGIN account so the strategy can also practice shorting in simulation.
    # (Real Binance *spot* cannot short — use allow_short=False there.)
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(STARTING_CASH, USDT)],
        base_currency=USDT,
    )
    engine.add_instrument(instrument)

    bars_df = load_bars()
    wrangler = BarDataWrangler(bar_type, instrument)
    bars = wrangler.process(bars_df)
    engine.add_data(bars)
    print(f"Loaded {len(bars):,} 1-minute bars (fake money only)")

    strategy = EMACross(
        EMACrossConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            trade_size=TRADE_SIZE,
            allow_short=True,  # simulation only; live spot should be False
            stop_loss_pct=STOP_LOSS_PCT,
            take_profit_pct=TAKE_PROFIT_PCT,
            daily_loss_limit_pct=DAILY_LOSS_LIMIT_PCT,
            news_calendar_path=(
                str(NEWS_CALENDAR) if NEWS_CALENDAR.exists() else ""
            ),
            news_blackout_minutes=NEWS_BLACKOUT_MINUTES,
        )
    )
    engine.add_strategy(strategy)

    print("Running backtest...")
    engine.run()

    # --- Results -------------------------------------------------------
    first_close = float(bars_df["close"].iloc[0])
    last_close = float(bars_df["close"].iloc[-1])
    buy_hold = STARTING_CASH * (last_close / first_close)
    final_balance = final_usdt_balance(engine)
    strat_ret = (
        (final_balance / STARTING_CASH - 1.0) if final_balance is not None else None
    )
    bh_ret = buy_hold / STARTING_CASH - 1.0

    print("\n=== RESULTS (paper money) ===")
    if final_balance is not None:
        print(
            f"  Strategy:     {STARTING_CASH:,.2f} -> {final_balance:,.2f} USDT "
            f"({strat_ret:+.2%})"
        )
    else:
        print("  Strategy:     (could not read final balance)")
    print(
        f"  Buy & Hold:   {STARTING_CASH:,.2f} -> {buy_hold:,.2f} USDT "
        f"({bh_ret:+.2%})"
    )
    if strat_ret is not None:
        winner = "Strategy" if strat_ret > bh_ret else "Buy & Hold"
        print(f"  Winner:       {winner}")
        if strat_ret < bh_ret:
            print(
                "  NOTE: just holding beat the bot on this data. "
                "That is NORMAL — most strategies do. Do not go live on this."
            )

    print(
        f"  Risk events:  stop-loss/take-profit fires={strategy.stops_hit}, "
        f"daily halts={strategy.daily_halts}, "
        f"news blackouts={strategy.news_events_avoided}"
    )
    print(
        f"  Guardrails:   SL={STOP_LOSS_PCT:.0%}  TP={TAKE_PROFIT_PCT:.0%}  "
        f"daily halt={DAILY_LOSS_LIMIT_PCT:.0%}  "
        f"news blackout=±{NEWS_BLACKOUT_MINUTES}m "
        f"({'on' if NEWS_CALENDAR.exists() else 'calendar missing'})"
    )

    engine.dispose()

    print(
        """
HOW TO READ THIS:
- Synthetic data only proves the CODE works. It is not evidence of edge.
- If Buy & Hold beats the bot, the bot is not ready — tune or scrap the idea.
- Next: `python fetch_data.py` for real history, then Binance testnet,
  then (much later) tiny real amounts with a daily loss cap.
"""
    )


if __name__ == "__main__":
    main()
