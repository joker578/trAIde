# trAIde

Local crypto trading bot starter built on **[NautilusTrader](https://github.com/nautechsystems/nautilus_trader)** — a professional, event-driven trading engine.

> ## ⚠️ Reality check (read this first)
>
> **This project does not print money.** No open-source bot does.
>
> - Month 1 goal: run a bot that **doesn't lose your money to bugs**, and learn.
> - Most retail algo traders **lose** after fees/slippage.
> - Paper trade → testnet → only then *tiny* real amounts with a daily loss cap.
> - Never enable **Withdraw** on an API key. Trade permission only + IP whitelist.
> - Never risk money you can't afford to lose (especially with futures/leverage).

## What's in the box

| File | What it does |
|---|---|
| `strategies/ema_cross.py` | Starter strategy: fast/slow EMA crossover (buy when trend turns up) |
| `run_backtest.py` | **Start here.** Backtest on synthetic data — zero keys, fake money |
| `run_testnet.py` | Runs the same strategy on **Binance testnet** (needs free testnet keys) |
| `.env.example` | Template for API keys (copy to `.env`, never commit `.env`) |

## Quickstart (backtest only, no keys needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_backtest.py
```

## Roadmap

1. **Now:** backtests (`run_backtest.py`) — tune the strategy, break it, learn
2. **Next:** Binance testnet (`run_testnet.py`) — live-like, fake money
3. **Later:** weeks of paper results + risk rules → *tiny* real allocation
4. **Optional:** add OpenBB for data, TradingAgents/Vibe-Trading as the "brain", OpenTerminal as the dashboard

## From the original repo list

- **Using:** nautilus_trader (engine), maybe OpenBB / skfolio / OpenTerminal later
- **Skipping for now:** browser-use & camofox (we use official APIs, not browser clicks), valuecell (nautilus handles execution), TradingAgents/Vibe-Trading (add later as optional brains)

## License / disclaimer

Educational project. Not financial advice. You are responsible for anything this bot does with your account.
