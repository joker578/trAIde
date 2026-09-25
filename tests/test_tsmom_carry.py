"""
TSMOM + carry engine tests (no pytest needed).

Run:  .venv/bin/python tests/test_tsmom_carry.py
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402

from strategies.tsmom import TSMOM, TSMOMConfig  # noqa: E402


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_tsmom_config() -> None:
    cfg = TSMOMConfig(
        instrument_id=InstrumentId.from_str("BTCUSDT.BINANCE"),
        bar_type=BarType.from_str("BTCUSDT.BINANCE-1-HOUR-LAST-EXTERNAL"),
        trade_size=Decimal("0.01"),
        use_vol_sizing=True,
        lookback_bars=480,
        target_annual_vol=0.20,
    )
    strat = TSMOM(cfg)
    assert strat.config.use_vol_sizing is True
    assert strat.config.lookback_bars == 480
    print("  PASS tsmom_config")


def test_tsmom_backtest() -> None:
    proc = _run(["run_backtest.py", "--strategy", "tsmom"])
    assert proc.returncode == 0, (proc.stderr or "")[-2500:]
    out = proc.stdout
    assert "RESULTS [TSMOM]" in out, out[-2000:]
    assert "EXPECTANCY/trade" in out
    assert "vol target=" in out
    # must actually trade (warmup + signals)
    trades_line = [l for l in out.splitlines() if l.strip().startswith("Trades:")]
    assert trades_line, out[-2000:]
    n = int(trades_line[0].split(":")[1].strip())
    assert n >= 5, f"TSMOM produced too few trades: {n}"
    print(f"  PASS tsmom_backtest: {n} trades")


def test_emacross_still_default() -> None:
    proc = _run(["run_backtest.py"])
    assert proc.returncode == 0, (proc.stderr or "")[-2500:]
    assert "RESULTS [EMACROSS]" in proc.stdout
    print("  PASS emacross_still_default")


def test_carry_backtest_demo() -> None:
    proc = _run(["carry_backtest.py", "--demo"])
    assert proc.returncode == 0, (proc.stderr or "")[-2500:]
    out = proc.stdout
    assert "NET Ann" in out, out[-2000:]
    assert "Always on" in out
    assert "Threshold" in out
    print("  PASS carry_backtest_demo")


def test_run_carry_demo() -> None:
    proc = _run(["run_carry.py", "--demo", "--once"])
    assert proc.returncode == 0, (proc.stderr or "")[-2500:]
    out = proc.stdout
    assert "VERDICT" in out, out[-2000:]
    assert "annualized" in out
    print("  PASS run_carry_demo")


if __name__ == "__main__":
    print("Running TSMOM + carry tests...")
    test_tsmom_config()
    test_tsmom_backtest()
    test_emacross_still_default()
    test_carry_backtest_demo()
    test_run_carry_demo()
    print("ALL TESTS PASSED ✅")
