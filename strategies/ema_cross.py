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

This is a learning template, NOT a guaranteed money maker.
Always backtest + paper trade before risking real funds.
"""

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

    # ------------------------------------------------------------------ setup
    def on_start(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.subscribe_bars(self.config.bar_type)

    def on_order_filled(self, fill: OrderFilled) -> None:
        if fill.instrument_id != self.config.instrument_id:
            return
        # Only remember an entry price when we are actually holding a position
        # (a sell that merely CLOSES a long must not be recorded as a short entry).
        if self.portfolio.is_net_long(self.config.instrument_id) and fill.is_buy:
            self._long_entry = float(fill.last_px)
            self._short_entry = None
        elif self.portfolio.is_net_short(self.config.instrument_id) and fill.is_sell:
            self._short_entry = float(fill.last_px)
            self._long_entry = None
        elif self.portfolio.is_flat(self.config.instrument_id):
            self._long_entry = None
            self._short_entry = None

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

    # ----------------------------------------------------------- main logic
    def on_bar(self, bar: Bar) -> None:
        if not self.indicators_initialized():
            return

        self._update_daily_state(bar)

        # Kill switch active: stay flat, no new signals.
        if self._halted_today:
            if not self.portfolio.is_flat(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
            return

        # Guardrails first — they override the signal.
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
            f"Stopped. stop_loss_events={self.stops_hit} "
            f"daily_halts={self.daily_halts}"
        )
