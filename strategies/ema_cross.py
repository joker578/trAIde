"""
EMACross — starter strategy for NautilusTrader, with guardrails.

How it works (kid version):
- Draw two speed-lines on the chart: a fast one (short average) and a slow one.
- Fast line crosses ABOVE slow line -> trend turning up -> BUY.
- Fast line crosses BELOW slow line -> trend turning down -> SELL/exit.

Guardrails (the part that keeps you solvent):
- STOP LOSS: exit automatically if the trade loses too much.
- TAKE PROFIT: bank profit when the trade wins enough (default 2:1 reward:risk).
- DAILY LOSS LIMIT: if the account drops too much vs the start of the UTC day,
  the strategy closes everything and stops trading until the next day.
- NEWS BLACKOUT: if a news calendar CSV is provided, go flat and take no trades
  within +/- N minutes of high-impact events (PPI, CPI, NFP, FOMC).

Also logs every completed trade (entry/exit price + return) so tools like
news_event_study.py can measure win rates around news.

This is a learning template, NOT a guaranteed money maker.
Always backtest + paper trade before risking real funds.
"""

import bisect
import csv
from datetime import datetime
from decimal import Decimal

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

NANOS_PER_DAY = 86_400_000_000_000
NANOS_PER_MIN = 60_000_000_000


class EMACrossConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal
    fast_ema_period: int = 10
    slow_ema_period: int = 20
    # False = spot mode: only trade what you own (no shorting).
    # True = margin/futures mode: allowed to bet against the market too.
    allow_short: bool = True
    # Risk guardrails (as fractions: 0.02 = 2%)
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    daily_loss_limit_pct: float = 0.03
    # News blackout: path to calendar CSV ("" = disabled)
    news_calendar_path: str = ""
    news_blackout_minutes: int = 30


class EMACross(Strategy):
    def __init__(self, config: EMACrossConfig):
        super().__init__(config)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

        self._long_entry: float | None = None
        self._short_entry: float | None = None
        self._day_index: int | None = None
        self._day_start_equity: float | None = None
        self._halted_today: bool = False
        self.stops_hit = 0
        self.daily_halts = 0

        # News blackout state
        self._news_nanos: list[int] = []  # sorted event times
        self._active_news_nano: int | None = None  # event currently blocking
        self.news_blacked_out_bars = 0
        self.news_events_avoided = 0

        # Trade log (completed round-trips) for event studies
        self._open_trade: dict | None = None
        self.closed_trades: list[dict] = []

    # ------------------------------------------------------------------ setup
    def on_start(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.subscribe_bars(self.config.bar_type)
        self._load_news_calendar()

    def _load_news_calendar(self) -> None:
        path = self.config.news_calendar_path
        if not path:
            return
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    ts = datetime.fromisoformat(row["timestamp"])
                    self._news_nanos.append(int(ts.timestamp() * 1_000_000_000))
            self._news_nanos.sort()
            self.log.info(
                f"News calendar loaded: {len(self._news_nanos)} events from {path} "
                f"(+/- {self.config.news_blackout_minutes} min blackout)"
            )
        except OSError as exc:
            self.log.error(f"Could not load news calendar '{path}': {exc}")

    def _news_event_near(self, ts: int) -> int | None:
        """Return the event nanos if ts is inside a blackout window, else None."""
        if not self._news_nanos:
            return None
        margin = self.config.news_blackout_minutes * NANOS_PER_MIN
        i = bisect.bisect_left(self._news_nanos, ts - margin)
        if i < len(self._news_nanos) and self._news_nanos[i] <= ts + margin:
            return self._news_nanos[i]
        return None

    # ----------------------------------------------------------- trade log
    def on_order_filled(self, fill: OrderFilled) -> None:
        if fill.instrument_id != self.config.instrument_id:
            return

        commission = 0.0
        if fill.commission is not None:
            try:
                commission = float(fill.commission)
            except (TypeError, ValueError):
                commission = 0.0

        # Entry-price tracking for SL/TP
        if self.portfolio.is_net_long(self.config.instrument_id) and fill.is_buy:
            self._long_entry = float(fill.last_px)
            self._short_entry = None
        elif self.portfolio.is_net_short(self.config.instrument_id) and fill.is_sell:
            self._short_entry = float(fill.last_px)
            self._long_entry = None
        elif self.portfolio.is_flat(self.config.instrument_id):
            self._long_entry = None
            self._short_entry = None

        # Round-trip trade log
        flat = self.portfolio.is_flat(self.config.instrument_id)
        long_ = self.portfolio.is_net_long(self.config.instrument_id)
        short_ = self.portfolio.is_net_short(self.config.instrument_id)

        if self._open_trade is None:
            if long_ or short_:
                self._open_trade = {
                    "side": "LONG" if long_ else "SHORT",
                    "entry_px": float(fill.last_px),
                    "entry_ts": fill.ts_event,
                    "commission": commission,
                }
        else:
            self._open_trade["commission"] += commission
            if flat:
                exit_px = float(fill.last_px)
                entry = self._open_trade["entry_px"]
                if self._open_trade["side"] == "LONG":
                    ret = exit_px / entry - 1.0
                else:
                    ret = entry / exit_px - 1.0
                self.closed_trades.append(
                    {
                        **self._open_trade,
                        "exit_px": exit_px,
                        "exit_ts": fill.ts_event,
                        "return_pct": ret,
                    }
                )
                self._open_trade = None

    # ------------------------------------------------------------ risk layer
    def _equity(self) -> float:
        account = self.portfolio.account(self.config.instrument_id.venue)
        balance = account.balance_total(USDT)
        equity = float(balance) if balance is not None else 0.0
        upnl = self.portfolio.unrealized_pnl(self.config.instrument_id)
        if upnl is not None:
            equity += float(upnl)
        return equity

    def _update_daily_state(self, bar: Bar) -> None:
        day = bar.ts_event // NANOS_PER_DAY
        if day != self._day_index:
            self._day_index = day
            self._halted_today = False
            self._day_start_equity = self._equity()
        if self._halted_today or self._day_start_equity is None:
            return
        equity = self._equity()
        drop = 1.0 - (equity / self._day_start_equity)
        if drop >= self.config.daily_loss_limit_pct:
            self._halted_today = True
            self.daily_halts += 1
            self.log.warning(
                f"DAILY LOSS LIMIT HIT ({drop:.2%} <= -"
                f"{self.config.daily_loss_limit_pct:.2%}) — "
                "closing positions, no new trades until tomorrow (UTC)."
            )
            if not self.portfolio.is_flat(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)

    def _check_stop_loss_and_take_profit(self, bar: Bar) -> bool:
        """Return True if a guardrail fired (caller should not re-enter this bar)."""
        close = float(bar.close)
        instrument = self.config.instrument_id

        if self.portfolio.is_net_long(instrument) and self._long_entry:
            stop = self._long_entry * (1.0 - self.config.stop_loss_pct)
            target = self._long_entry * (1.0 + self.config.take_profit_pct)
            if close <= stop:
                self.stops_hit += 1
                self.log.warning(
                    f"STOP LOSS (long) entry={self._long_entry:.2f} "
                    f"close={close:.2f} stop={stop:.2f}"
                )
                self.close_all_positions(instrument)
                return True
            if close >= target:
                self.log.info(
                    f"TAKE PROFIT (long) entry={self._long_entry:.2f} "
                    f"close={close:.2f} target={target:.2f}"
                )
                self.close_all_positions(instrument)
                return True

        if self.portfolio.is_net_short(instrument) and self._short_entry:
            stop = self._short_entry * (1.0 + self.config.stop_loss_pct)
            target = self._short_entry * (1.0 - self.config.take_profit_pct)
            if close >= stop:
                self.stops_hit += 1
                self.log.warning(
                    f"STOP LOSS (short) entry={self._short_entry:.2f} "
                    f"close={close:.2f} stop={stop:.2f}"
                )
                self.close_all_positions(instrument)
                return True
            if close <= target:
                self.log.info(
                    f"TAKE PROFIT (short) entry={self._short_entry:.2f} "
                    f"close={close:.2f} target={target:.2f}"
                )
                self.close_all_positions(instrument)
                return True

        return False

    def _apply_news_blackout(self, bar: Bar) -> bool:
        """True = we are inside a news blackout window (no trading allowed)."""
        event = self._news_event_near(bar.ts_event)
        if event is None:
            self._active_news_nano = None
            return False
        if self._active_news_nano != event:
            self._active_news_nano = event
            self.news_events_avoided += 1
            when = datetime.utcfromtimestamp(event / 1_000_000_000)
            self.log.warning(
                f"NEWS BLACKOUT active around {when.isoformat()}Z — "
                "closing positions, no new trades."
            )
        self.news_blacked_out_bars += 1
        if not self.portfolio.is_flat(self.config.instrument_id):
            self.close_all_positions(self.config.instrument_id)
        return True

    # ----------------------------------------------------------- main logic
    def on_bar(self, bar: Bar) -> None:
        if not self.indicators_initialized():
            return

        # News blackout is checked FIRST — it overrides everything.
        if self._apply_news_blackout(bar):
            return

        self._update_daily_state(bar)

        # Kill switch active: stay flat, no new signals.
        if self._halted_today:
            if not self.portfolio.is_flat(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
            return

        # Guardrails — they override the signal.
        if self._check_stop_loss_and_take_profit(bar):
            return

        # EMA crossover signal
        if self.fast_ema.value >= self.slow_ema.value:
            if self.portfolio.is_flat(self.config.instrument_id):
                self.buy()
            elif self.portfolio.is_net_short(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
                self.buy()
        elif self.fast_ema.value < self.slow_ema.value:
            if self.portfolio.is_net_long(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
                if self.config.allow_short:
                    self.sell()
            elif self.config.allow_short and self.portfolio.is_flat(
                self.config.instrument_id
            ):
                self.sell()

    # ------------------------------------------------------------- orders
    def buy(self) -> None:
        instrument = self.cache.instrument(self.config.instrument_id)
        order = self.order_factory.market(
            self.config.instrument_id,
            OrderSide.BUY,
            instrument.make_qty(self.config.trade_size),
        )
        self.submit_order(order)

    def sell(self) -> None:
        instrument = self.cache.instrument(self.config.instrument_id)
        order = self.order_factory.market(
            self.config.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(self.config.trade_size),
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
        self.log.info(
            f"Stopped. news_blackout_bars={self.news_blacked_out_bars} "
            f"news_events_avoided={self.news_events_avoided} "
            f"closed_trades={len(self.closed_trades)}"
        )
