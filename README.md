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

| File | What it does |
|---|---|
| `strategies/ema_cross.py` | EMA crossover strategy **with guardrails**: stop-loss, take-profit, daily loss kill-switch |
| `run_backtest.py` | **Start here.** Backtests on real CSV or synthetic data — zero keys, fake money. Compares vs buy-and-hold |
| `fetch_data.py` | Downloads real BTCUSDT 1m history from Binance public API (no key) into `data/` |
| `run_testnet.py` | Runs the strategy on **Binance testnet** (needs free testnet keys) |
| `.env.example` | Template for API keys (copy to `.env`, never commit `.env`) |

## Risk guardrails (built into the strategy)

| Control | Default | What it does |
|---|---|---|
| Stop-loss | 2% | Auto-exit a losing trade at −2% |
| Take-profit | 4% | Bank a winning trade at +4% (2:1 reward:risk) |
| Daily loss limit | 3% | If down 3% vs start of UTC day → close everything, **no new trades until tomorrow** |

Tune these in `run_backtest.py` (`STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `DAILY_LOSS_LIMIT_PCT`).

## Quickstart (backtest only, no keys needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_backtest.py              # synthetic data, works offline
.venv/bin/python fetch_data.py 30             # optional: real 30-day history
.venv/bin/python run_backtest.py              # now uses data/btcusdt_1m.csv
```

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
