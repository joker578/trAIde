"""
EMACross — starter strategy for NautilusTrader, with guardrails.

How it works (kid version):
- Draw two speed-lines on the chart: a fast one (short average) and a slow one.
- Fast line crosses ABOVE slow line -> trend turning up -> BUY.
- Fast line crosses BELOW slow line -> trend turning down -> SELL/exit.

Guardrails (the part that keeps you solvent):
- STOP LOSS: exit automatically if the trade loses too much.
- TRAILING STOP: once a trade is in profit, the exit trails behind the best
  price reached (lets winners run) — fixed take-profit is the fallback.
- POSITION RISK: every trade risks a fixed % of equity (default 1%).
  Size = risk_amount / stop_distance, capped so one trade never exceeds
  max_notional_frac of the account.
- DAILY LOSS LIMIT: down too much vs start of UTC day -> flat until tomorrow.
- NEWS BLACKOUT: +/- N minutes around PPI/CPI/NFP/FOMC -> flat, no trades.

Logs every completed trade (qty, fees, net return) — the scoreboard uses
EXPECTANCY (avg net edge per trade), not win rate.

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
BINANCE_MIN_NOTIONAL_USDT = 10.0


class EMACrossConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal  # fallback fixed size if risk sizing is disabled
    fast_ema_period: int = 10
    slow_ema_period: int = 20
    # False = spot mode: only trade what you own (no shorting).
    # True = margin/futures mode: allowed to bet against the market too.
    allow_short: bool = True
    # --- Risk guardrails (fractions: 0.02 = 2%) ---
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04  # used only when trailing_stop_pct == 0
    daily_loss_limit_pct: float = 0.03
    # --- Position sizing: fixed-fractional (risk X% of equity per trade) ---
    risk_per_trade_pct: float = 0.01  # lose at most 1% of equity if stopped
    max_notional_frac: float = 0.5  # never use more than 50% of equity notional
    use_risk_sizing: bool = True
    # --- Trailing stop (lets winners run; 0 = use fixed take-profit) ---
    trailing_stop_pct: float = 0.015
    # --- News blackout ---
    news_calendar_path: str = ""
    news_blackout_minutes: int = 30


class EMACross(Strategy):
    def __init__(self, config: EMACrossConfig):
        super().__init__(config)
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

        self._long_entry: float | None = None
        self._short_entry: float | None = None
        self._trail_peak: float | None = None  # best price since entry (long)
        self._trail_trough: float | None = None  # worst price since entry (short)
        self._day_index: int | None = None
        self._day_start_equity: float | None = None
        self._halted_today: bool = False
        self.stops_hit = 0
        self.trailing_exits = 0
        self.daily_halts = 0

        # News blackout state
        self._news_nanos: list[int] = []
        self._active_news_nano: int | None = None
        self.news_blacked_out_bars = 0
        self.news_events_avoided = 0

        # Trade log (completed round-trips) for the expectancy scoreboard
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

        # Entry-price tracking for SL/TP/trailing
        if self.portfolio.is_net_long(self.config.instrument_id) and fill.is_buy:
            self._long_entry = float(fill.last_px)
            self._short_entry = None
            self._trail_peak = float(fill.last_px)
            self._trail_trough = None
        elif self.portfolio.is_net_short(self.config.instrument_id) and fill.is_sell:
            self._short_entry = float(fill.last_px)
            self._long_entry = None
            self._trail_trough = float(fill.last_px)
            self._trail_peak = None
        elif self.portfolio.is_flat(self.config.instrument_id):
            self._long_entry = None
            self._short_entry = None
            self._trail_peak = None
            self._trail_trough = None

        # Round-trip trade log (fees included so expectancy can be net)
        flat = self.portfolio.is_flat(self.config.instrument_id)
        long_ = self.portfolio.is_net_long(self.config.instrument_id)
        short_ = self.portfolio.is_net_short(self.config.instrument_id)

        if self._open_trade is None:
            if long_ or short_:
                self._open_trade = {
                    "side": "LONG" if long_ else "SHORT",
                    "entry_px": float(fill.last_px),
                    "entry_ts": fill.ts_event,
                    "qty": float(fill.last_qty),
                    "commission": commission,
                }
        else:
            self._open_trade["commission"] += commission
            if flat:
                exit_px = float(fill.last_px)
                entry = self._open_trade["entry_px"]
                if self._open_trade["side"] == "LONG":
                    gross = exit_px / entry - 1.0
                else:
                    gross = entry / exit_px - 1.0
                notional = entry * self._open_trade["qty"]
                fee_frac = (
                    self._open_trade["commission"] / notional if notional > 0 else 0.0
                )
                self.closed_trades.append(
                    {
                        **self._open_trade,
                        "exit_px": exit_px,
                        "exit_ts": fill.ts_event,
                        "gross_pct": gross,
                        "fee_frac": fee_frac,
                        "return_pct": gross - fee_frac,  # NET of fees
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
        """Hard SL, then trailing/fixed exit. True = fired (no re-entry)."""
        close = float(bar.close)
        instrument = self.config.instrument_id
        trail = self.config.trailing_stop_pct

        if self.portfolio.is_net_long(instrument) and self._long_entry:
            hard_stop = self._long_entry * (1.0 - self.config.stop_loss_pct)
            if close <= hard_stop:
                self.stops_hit += 1
                self.log.warning(
                    f"STOP LOSS (long) entry={self._long_entry:.2f} "
                    f"close={close:.2f} stop={hard_stop:.2f}"
                )
                self.close_all_positions(instrument)
                return True

            if trail > 0.0 and self._trail_peak is not None:
                self._trail_peak = max(self._trail_peak, close)
                trail_stop = self._trail_peak * (1.0 - trail)
                if close <= trail_stop and trail_stop > hard_stop:
                    self.trailing_exits += 1
                    self.log.info(
                        f"TRAIL STOP (long) peak={self._trail_peak:.2f} "
                        f"close={close:.2f} trail={trail_stop:.2f}"
                    )
                    self.close_all_positions(instrument)
                    return True
            elif trail == 0.0:
                target = self._long_entry * (1.0 + self.config.take_profit_pct)
                if close >= target:
                    self.log.info(
                        f"TAKE PROFIT (long) entry={self._long_entry:.2f} "
                        f"close={close:.2f} target={target:.2f}"
                    )
                    self.close_all_positions(instrument)
                    return True

        if self.portfolio.is_net_short(instrument) and self._short_entry:
            hard_stop = self._short_entry * (1.0 + self.config.stop_loss_pct)
            if close >= hard_stop:
                self.stops_hit += 1
                self.log.warning(
                    f"STOP LOSS (short) entry={self._short_entry:.2f} "
                    f"close={close:.2f} stop={hard_stop:.2f}"
                )
                self.close_all_positions(instrument)
                return True

            if trail > 0.0 and self._trail_trough is not None:
                self._trail_trough = min(self._trail_trough, close)
                trail_stop = self._trail_trough * (1.0 + trail)
                if close >= trail_stop and trail_stop < hard_stop:
                    self.trailing_exits += 1
                    self.log.info(
                        f"TRAIL STOP (short) trough={self._trail_trough:.2f} "
                        f"close={close:.2f} trail={trail_stop:.2f}"
                    )
                    self.close_all_positions(instrument)
                    return True
            elif trail == 0.0:
                target = self._short_entry * (1.0 - self.config.take_profit_pct)
                if close <= target:
                    self.log.info(
                        f"TAKE PROFIT (short) entry={self._short_entry:.2f} "
                        f"close={close:.2f} target={target:.2f}"
                    )
                    self.close_all_positions(instrument)
                    return True

        return False

    def _apply_news_blackout(self, bar: Bar) -> bool:
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

    # ------------------------------------------------------- position sizing
    def _trade_qty(self, price: float) -> Decimal | None:
        """
        Fixed-fractional sizing: risk exactly risk_per_trade_pct of equity
        on this trade (loss if stopped = risk amount). Capped by max notional.
        """
        instrument = self.cache.instrument(self.config.instrument_id)
        if not self.config.use_risk_sizing:
            qty = instrument.make_qty(self.config.trade_size)
            if float(qty) * price < BINANCE_MIN_NOTIONAL_USDT:
                return None
            return qty

        equity = self._equity()
        if equity <= 0:
            return None
        stop_dist = price * self.config.stop_loss_pct
        if stop_dist <= 0:
            return None

        risk_usdt = equity * self.config.risk_per_trade_pct
        qty_f = risk_usdt / stop_dist
        max_qty = (equity * self.config.max_notional_frac) / price
        qty_f = min(qty_f, max_qty)

        qty = instrument.make_qty(Decimal(str(qty_f)))
        if qty <= 0:
            return None
        if float(qty) * price < BINANCE_MIN_NOTIONAL_USDT:
            return None  # below exchange min notional — skip, not a free trade
        return qty

    # ----------------------------------------------------------- main logic
    def on_bar(self, bar: Bar) -> None:
        if not self.indicators_initialized():
            return

        # News blackout is checked FIRST — it overrides everything.
        if self._apply_news_blackout(bar):
            return

        self._update_daily_state(bar)

        if self._halted_today:
            if not self.portfolio.is_flat(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
            return

        if self._check_stop_loss_and_take_profit(bar):
            return

        price = float(bar.close)

        # EMA crossover signal
        if self.fast_ema.value >= self.slow_ema.value:
            if self.portfolio.is_flat(self.config.instrument_id):
                self.buy(price)
            elif self.portfolio.is_net_short(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
                self.buy(price)
        elif self.fast_ema.value < self.slow_ema.value:
            if self.portfolio.is_net_long(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
                if self.config.allow_short:
                    self.sell(price)
            elif self.config.allow_short and self.portfolio.is_flat(
                self.config.instrument_id
            ):
                self.sell(price)

    # ------------------------------------------------------------- orders
    def buy(self, price: float) -> None:
        qty = self._trade_qty(price)
        if qty is None:
            return
        order = self.order_factory.market(
            self.config.instrument_id,
            OrderSide.BUY,
            qty,
        )
        self.submit_order(order)

    def sell(self, price: float) -> None:
        qty = self._trade_qty(price)
        if qty is None:
            return
        order = self.order_factory.market(
            self.config.instrument_id,
            OrderSide.SELL,
            qty,
        )
        self.submit_order(order)

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
        self.log.info(
            f"Stopped. news_blackout_bars={self.news_blacked_out_bars} "
            f"news_events_avoided={self.news_events_avoided} "
            f"closed_trades={len(self.closed_trades)}"
        )
