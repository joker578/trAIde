"""
Backtest (paper money on historical data) — run this FIRST.

Usage:
    .venv/bin/python run_backtest.py

By default it generates synthetic BTC/USDT-style 1-minute bars so it works
offline with zero API keys. No real money is involved, ever.
"""

from __future__ import annotations

from decimal import Decimal

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

N_BARS = 5_000  # ~3.5 days of 1-minute bars
STARTING_CASH = 10_000  # fake USDT
TRADE_SIZE = Decimal("0.01")  # BTC per trade (tiny — risk management matters)


def make_synthetic_bars(n: int = N_BARS, seed: int = 42) -> pd.DataFrame:
    """Generate a realistic-looking random walk with trending regimes."""
    rng = np.random.default_rng(seed)
    # Slowly swinging drift creates trends the EMA cross can actually catch
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

    bars_df = make_synthetic_bars()
    wrangler = BarDataWrangler(bar_type, instrument)
    bars = wrangler.process(bars_df)
    engine.add_data(bars)
    print(f"Loaded {len(bars):,} synthetic 1-minute bars (fake money only)")

    strategy = EMACross(
        EMACrossConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            trade_size=TRADE_SIZE,
        )
    )
    engine.add_strategy(strategy)

    print("Running backtest...")
    engine.run()

    # --- Results ---
    print("\n=== RESULTS (paper money) ===")
    try:
        result = engine.get_result()
        # Print whatever stats the engine exposes for this version
        stats = getattr(result, "stats_returns", None) or getattr(result, "stats", None)
        if stats:
            for key, value in (stats.items() if isinstance(stats, dict) else []):
                print(f"  {key}: {value}")
        else:
            print(result)
    except Exception as exc:  # API surface changes between versions
        print(f"(Could not pull structured stats: {exc})")

    try:
        report = engine.trader.generate_account_report(Venue("BINANCE"))
        print("\nFinal account balance:")
        print(report.tail(3).to_string())
    except Exception:
        pass

    engine.dispose()

    print(
        """
HOW TO READ THIS:
- If PnL is positive on synthetic data, that only means the code works.
- It does NOT mean you will make money live. Markets are not this kind.
- Next steps: try real historical CSVs, then Binance testnet, then (much later)
  tiny real amounts with a daily loss cap.
"""
    )


if __name__ == "__main__":
    main()
