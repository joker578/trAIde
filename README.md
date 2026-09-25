# trAIde

Local crypto trading bot starter built on **[NautilusTrader](https://github.com/nautechsystems/nautilus_trader)** — a professional, event-driven trading engine used by real quant teams.

> ## ⚠️ Reality check (read this first)
>
> **This project does not print money.** No open-source bot does.
>
> - Month 1 goal: run a bot that **doesn't lose your money to bugs**, and learn.
> - Most retail algo traders **lose** after fees/slippage.
> - Backtest → paper/testnet → only then *tiny* real amounts with a daily loss cap.
> - Never enable **Withdraw** on an API key. Trade permission only + IP whitelist.
> - Never risk money you can't afford to lose (especially with futures/leverage).

## What's in the box

## Two engines (the pro structure)

| Engine | File | What it does | Evidence family |
|---|---|---|---|
| **Trend** | `strategies/ema_cross.py`, `strategies/tsmom.py` | Directional: long/short BTC based on trend signals | Trend following / TSMOM (137yr) |
| **Carry** | `carry_backtest.py`, `run_carry.py` | Market-neutral: long spot + short perp, harvest funding | Carry (AQR century factor) |

They earn money from **different sources** and are (mostly) uncorrelated — running
both with separate risk budgets is the actual "quant fund" structure.

```bash
# Trend engine
.venv/bin/python run_backtest.py                     # EMA cross (1m)
.venv/bin/python run_backtest.py --strategy tsmom    # TSMOM + vol targeting (1h)

# Carry engine
.venv/bin/python fetch_funding.py                    # real funding history (your machine)
.venv/bin/python carry_backtest.py --demo            # demo carry math (offline)
.venv/bin/python run_carry.py --demo                 # monitor preview
.venv/bin/python run_carry.py --once                 # live verdict (no keys needed)
```

## Files

| File | What it does |
|---|---|
| `strategies/ema_cross.py` | EMA cross + guardrails: SL, trailing stop, daily kill-switch, news blackout, **1% risk sizing** |
| `strategies/tsmom.py` | **TSMOM**: sign-of-returns signal + **vol targeting** (20% ann. target), inherits all guardrails |
| `run_backtest.py` | Backtester: `--strategy emacross\|tsmom`, net of fees+slippage, expectancy scoreboard |
| `walk_forward.py` | Honest out-of-sample validator (train-select, test-validate) |
| `fetch_data.py` / `fetch_funding.py` | Download real 1m candles / funding history (public APIs, no keys) |
| `carry_backtest.py` | Funding carry backtest: always-on vs threshold modes, fees, droughts |
| `run_carry.py` | Live carry **monitor** (public data, no keys) — prints OPEN/STAND ASIDE verdicts |
| `news_event_study.py` | Bot win rate BEFORE vs AFTER PPI/CPI/NFP/FOMC vs quiet-day control |
| `config/news_calendar.csv` | 89 official BLS+Fed events (UTC, DST-correct) |
| `run_testnet.py` | Binance **testnet** runner (`testnet=True` hardcoded) |
| `tests/` | 12 tests: news(4) + walk-forward(3) + tsmom/carry(5) |

## Risk guardrails (built into the strategy)

| Control | Default | What it does |
|---|---|---|
| Stop-loss | 2% | Auto-exit a losing trade at −2% |
| Trailing stop | 1.5% | Exit trails the best price reached — **lets winners run** (fixed 4% TP is the fallback) |
| Position risk | **1% of equity** | Every trade risks exactly 1% of the account (fixed-fractional sizing), notional capped at 50% |
| Daily loss limit | 3% | If down 3% vs start of UTC day → close everything, **no new trades until tomorrow** |
| **News blackout** | **±30 min** | **Go flat and trade nothing around PPI / CPI / NFP / FOMC releases** |
| Costs in backtest | **0.10%/side + 1 tick slippage** | Backtests are **net of fees** — no more fake profits |

**The scoreboard metric is EXPECTANCY per trade (net of fees), not win rate.**
A 40% win rate with 3:1 reward:risk beats 90% with 1:10. The backtest prints
expectancy, profit factor, and avg win/loss — win rate is shown as the
"vanity metric" it is.

Tune these in `run_backtest.py`.

## News: why the blackout exists

Research shows macro releases (CPI/PPI/FOMC/NFP) are where most of the market's
movement comes from — and where retail bots get slaughtered by spreads (5–20× wider),
slippage, and fake first moves. This bot **sits those out** instead of trying to
out-run millisecond algos.

To **measure** how a naive bot (no blackout) actually performs around news:

```bash
.venv/bin/python fetch_data.py 730            # real 2 years of 1m candles (on your machine)
.venv/bin/python news_event_study.py --kinds PPI    # win rate: PRE vs POST vs CONTROL
.venv/bin/python news_event_study.py                # all event kinds
.venv/bin/python news_event_study.py --demo         # no data? verifies the pipeline only
```

## Quickstart (backtest only, no keys needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_backtest.py              # synthetic data, works offline
.venv/bin/python fetch_data.py 30             # optional: real 30-day history
.venv/bin/python run_backtest.py              # now uses data/btcusdt_1m.csv
.venv/bin/python walk_forward.py --data data/btcusdt_1m.csv  # honest OOS test
```

## Walk-forward: the only backtest that can't lie

`walk_forward.py` splits history into rolling train/test windows:

1. On **train**: grid-search (fast/slow EMA × trend-filter on/off) by expectancy
2. On **test**: run only the winner on unseen data (out-of-sample)
3. Roll forward and repeat — **aggregate TEST expectancy is the honest number**

Rule: if optimized OOS doesn't beat the static baseline (10/50+TF), the grid is
curve-fitting — use static params. If most test windows aren't positive, don't
go to testnet, let alone live.

Trend filter (200-EMA): long only above, short only below — enabled by default
in `run_backtest.py` (`USE_TREND_FILTER`).

**How to judge results honestly:**
- If the bot loses to **Buy & Hold**, it is not ready. Do not go live.
- Synthetic-data wins only mean the code runs — not that you have an edge.
- Look for: survives drawdowns, beats buy-and-hold *on multiple time periods*, risk events behave sanely.

## Roadmap

1. **Now:** backtests — tune, break, learn (`run_backtest.py`)
2. **Next:** Binance testnet (`run_testnet.py`) — live-like, fake money
3. **Later:** weeks of testnet results + written risk rules → *tiny* real allocation
4. **Optional:** OpenBB for data, TradingAgents/Vibe-Trading as an optional "brain", OpenTerminal as dashboard

## Binance testnet

1. Get free keys: [spot testnet](https://testnet.binance.vision/) or [futures testnet](https://testnet.binancefuture.com/)
2. `cp .env.example .env` and paste the **testnet** keys
3. `.venv/bin/python run_testnet.py`

Safety hardcoding: `run_testnet.py` forces `testnet=True`. Do not remove that until you know exactly why.

## From the original repo list

- **Using:** nautilus_trader (engine) — plus OpenBB / skfolio / OpenTerminal as optional add-ons later
- **Skipped:** browser-use & camofox (official APIs > browser clicks), valuecell (nautilus executes), TradingAgents/Vibe-Trading (optional brains, not core)

## License / disclaimer

Educational project. Not financial advice. You are responsible for anything this bot does with your account.
