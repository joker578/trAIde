"""
Verification tests for news blackout + event study (no pytest needed).

Run:  .venv/bin/python tests/test_news.py
"""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nautilus_trader.backtest.engine import BacktestEngine  # noqa: E402
from nautilus_trader.config import BacktestEngineConfig  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.model.currencies import USDT  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.enums import AccountType  # noqa: E402
from nautilus_trader.model.enums import OmsType  # noqa: E402
from nautilus_trader.model.identifiers import TraderId, Venue  # noqa: E402
from nautilus_trader.model.objects import Money  # noqa: E402
from nautilus_trader.persistence.wranglers import BarDataWrangler  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402

from strategies.ema_cross import EMACross, EMACrossConfig  # noqa: E402


def synth_bars(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.sin(np.linspace(0, 6 * np.pi, n)) * 0.0003
    close = 50_000.0 * np.exp(np.cumsum(drift + rng.normal(0, 0.0006, n)))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0, 0.0004, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(5, 50, n),
        },
        index=pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC"),
    )


def run_strategy(bars_df: pd.DataFrame, calendar_path: str) -> EMACross:
    instrument = TestInstrumentProvider.btcusdt_binance()
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("TEST-001"),
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10_000, USDT)],
        base_currency=USDT,
    )
    engine.add_instrument(instrument)
    engine.add_data(BarDataWrangler(bar_type, instrument).process(bars_df))
    strategy = EMACross(
        EMACrossConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            trade_size=Decimal("0.01"),
            news_calendar_path=calendar_path,
            news_blackout_minutes=30,
        )
    )
    engine.add_strategy(strategy)
    engine.run()
    engine.dispose()
    return strategy


def test_blackout_fires() -> None:
    """An event in the middle of the data must cause blackouts."""
    bars = synth_bars(5_000)  # Jan 1 .. Jan 4-ish 2025
    event_ts = datetime(2025, 1, 2, 12, 30, tzinfo=timezone.utc)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", newline="", delete=False
    ) as f:
        w = csv.DictWriter(
            f, fieldnames=["timestamp", "name", "kind", "importance"]
        )
        w.writeheader()
        w.writerow(
            {
                "timestamp": event_ts.isoformat(),
                "name": "PPI",
                "kind": "PPI",
                "importance": "high",
            }
        )
        path = f.name

    with_cal = run_strategy(bars, path)
    without_cal = run_strategy(bars, "")

    assert with_cal.news_events_avoided >= 1, "blackout never triggered"
    # 61 bars in +/-30min window (incl. event minute)
    assert with_cal.news_blacked_out_bars >= 60, (
        f"expected >=60 blocked bars, got {with_cal.news_blacked_out_bars}"
    )
    assert without_cal.news_events_avoided == 0
    assert without_cal.news_blacked_out_bars == 0
    print(
        f"  PASS blackout_fires: events_avoided={with_cal.news_events_avoided}, "
        f"blocked_bars={with_cal.news_blacked_out_bars}, "
        f"control_blocked={without_cal.news_blacked_out_bars}"
    )


def test_calendar_csv() -> None:
    """Shipped calendar must parse, be sorted, and contain the 4 kinds."""
    cal = ROOT / "config" / "news_calendar.csv"
    assert cal.exists(), "config/news_calendar.csv missing"
    rows = list(csv.DictReader(cal.open()))
    assert len(rows) >= 80, f"expected >=80 events, got {len(rows)}"
    kinds = {r["kind"] for r in rows}
    assert kinds == {"PPI", "CPI", "NFP", "FOMC"}, kinds
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps), "calendar not sorted"
    # DST sanity: winter PPI = 13:30Z, summer PPI = 12:30Z
    winter = [s for s in stamps if s.startswith("2025-01-14T")]
    summer = [s for s in stamps if s.startswith("2025-07-16T")]
    assert winter and winter[0].startswith("2025-01-14T13:30"), winter
    assert summer and summer[0].startswith("2025-07-16T12:30"), summer
    print(f"  PASS calendar_csv: {len(rows)} events, kinds={sorted(kinds)}, DST ok")


def test_trade_log() -> None:
    """Strategy must record completed round-trip trades."""
    bars = synth_bars(5_000)
    strat = run_strategy(bars, "")
    assert len(strat.closed_trades) >= 1, "no closed trades logged"
    t = strat.closed_trades[0]
    for key in ("side", "entry_px", "exit_px", "entry_ts", "exit_ts", "return_pct"):
        assert key in t, f"trade missing {key}"
    print(f"  PASS trade_log: {len(strat.closed_trades)} trades logged")


def test_event_study_demo() -> None:
    """The analyzer must run end-to-end in demo mode (PPI only, fast)."""
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "news_event_study.py"),
            "--kinds",
            "PPI",
            "--demo",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout
    assert "EVENT STUDY" in out, out[-1500:]
    assert "DEMO" in out
    assert "POST" in out and "CONTROL" in out
    print("  PASS event_study_demo: analyzer ran end-to-end")


if __name__ == "__main__":
    print("Running news feature tests...")
    test_calendar_csv()
    test_blackout_fires()
    test_trade_log()
    test_event_study_demo()
    print("ALL TESTS PASSED ✅")
