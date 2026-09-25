"""
Walk-forward + trend-filter tests (no pytest needed).

Run:  .venv/bin/python tests/test_walk_forward.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.ema_cross import EMACross, EMACrossConfig  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402
from decimal import Decimal  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402


def test_trend_filter_config() -> None:
    """Config fields must exist and freeze properly."""
    cfg = EMACrossConfig(
        instrument_id=InstrumentId.from_str("BTCUSDT.BINANCE"),
        bar_type=BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"),
        trade_size=Decimal("0.01"),
        use_trend_filter=True,
        trend_ema_period=200,
    )
    assert cfg.use_trend_filter is True
    assert cfg.trend_ema_period == 200
    strat = EMACross(cfg)
    assert strat.config.use_trend_filter is True
    print("  PASS trend_filter_config")


def test_walk_forward_demo() -> None:
    """Walk-forward must run end-to-end (tiny windows, fast)."""
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "walk_forward.py"),
            "--synth-days",
            "120",
            "--train-days",
            "25",
            "--test-days",
            "10",
            "--max-windows",
            "3",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (proc.stderr or "")[-2500:]
    out = proc.stdout
    assert "WALK-FORWARD RESULTS" in out, out[-2000:]
    assert "OOS (optimized)" in out, out[-2000:]
    assert "DEMO" in out
    print("  PASS walk_forward_demo: ran end-to-end with OOS aggregate")


def test_backtest_has_filter() -> None:
    """run_backtest must be wired for the trend filter (config check via grep)."""
    src = (ROOT / "run_backtest.py").read_text()
    assert "use_trend_filter=USE_TREND_FILTER" in src
    assert "USE_TREND_FILTER = True" in src
    assert "MakerTakerFeeModel" in src  # honest costs still on
    print("  PASS backtest_has_filter")


if __name__ == "__main__":
    print("Running walk-forward feature tests...")
    test_trend_filter_config()
    test_backtest_has_filter()
    test_walk_forward_demo()
    print("ALL TESTS PASSED ✅")
