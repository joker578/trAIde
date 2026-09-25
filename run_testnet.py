"""
Binance TESTNET trader — fake money on the real Binance test exchange.

You need TESTNET API keys first (not your real account keys):
  Spot testnet:     https://testnet.binance.vision/
  Futures testnet:  https://testnet.binancefuture.com/

Setup:
  cp .env.example .env   # then paste your TESTNET keys into .env
  set TRADING_MODE=testnet in .env

Run:
  .venv/bin/python run_testnet.py

SAFETY:
  - testnet=True is hardcoded below. Do NOT change it until you have
    paper-traded for weeks and passed your own risk rules.
  - Never enable Withdraw permission on any API key.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve() / ".env")

from nautilus_trader.adapters.binance import BinanceLiveDataClientFactory  # noqa: E402
from nautilus_trader.adapters.binance import BinanceLiveExecClientFactory  # noqa: E402
from nautilus_trader.config import TradingNodeConfig  # noqa: E402
from nautilus_trader.live.node import TradingNode  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402

from strategies.ema_cross import EMACross  # noqa: E402
from strategies.ema_cross import EMACrossConfig  # noqa: E402


def main() -> None:
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    account_type = os.getenv("BINANCE_ACCOUNT_TYPE", "spot")

    if not api_key or not api_secret:
        raise SystemExit(
            "Missing BINANCE_API_KEY / BINANCE_API_SECRET.\n"
            "Copy .env.example -> .env and paste TESTNET keys.\n"
            "Get them at https://testnet.binance.vision/ (spot) or "
            "https://testnet.binancefuture.com/ (futures)."
        )

    # Hardcoded safety: this script is TESTNET only.
    testnet = True

    config = TradingNodeConfig(
        trader_id="TRAIDE-TESTNET-001",
        data_clients={
            "BINANCE": {
                "api_key": api_key,
                "api_secret": api_secret,
                "account_type": account_type,  # spot | usdt_future
                "testnet": testnet,
            },
        },
        exec_clients={
            "BINANCE": {
                "api_key": api_key,
                "api_secret": api_secret,
                "account_type": account_type,
                "testnet": testnet,
            },
        },
    )

    node = TradingNode(config=config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)
    node.add_exec_client_factory("BINANCE", BinanceLiveExecClientFactory)

    instrument_id = InstrumentId.from_str("BTCUSDT.BINANCE")
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")

    calendar = Path(__file__).resolve() / "config" / "news_calendar.csv"

    strategy = EMACross(
        EMACrossConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            trade_size=Decimal("0.001"),  # tiny size on testnet
            # Spot accounts cannot short — only futures may.
            allow_short=(account_type == "usdt_future"),
            # Sit out PPI/CPI/NFP/FOMC releases
            news_calendar_path=str(calendar) if calendar.exists() else "",
            news_blackout_minutes=30,
        )
    )
    node.add_strategy(strategy)

    node.build()

    print("=" * 60)
    print("  trAIde -> Binance TESTNET (fake money)")
    print("  Instrument: BTCUSDT   Strategy: EMACross")
    print("  Ctrl+C to stop")
    print("=" * 60)

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
