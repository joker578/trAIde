"""
TSMOM — Time-Series Momentum with volatility targeting.

The professional version of the EMA bot, following the strategy family with
the longest evidence (Moskowitz-Ooi-Pedersen 2012; Hurst-Ooi-Pedersen 137yr):

- SIGNAL: trailing return over `lookback_bars` > 0 -> LONG, else SHORT.
  (The 1880-2016 studies used 12-month sign-of-return; we scale it down.)
- SIZING (vol targeting): position notional = equity * (target_vol / realized_vol),
  capped by max_notional_frac. Big volatility -> small position, calm -> bigger.
  (Barroso & Santa-Clara 2015: vol-scaling roughly doubles momentum Sharpe.)

Inherits ALL guardrails from EMACross: stop-loss, trailing stop, daily loss
kill-switch, news blackout, fee-aware trade log. Only the signal and the
sizing are different.
"""

from collections import deque
from decimal import Decimal
import math

from nautilus_trader.model.data import Bar

from strategies.ema_cross import EMACross, EMACrossConfig


class TSMOMConfig(EMACrossConfig):
    # Signal: sign of trailing return
    lookback_bars: int = 480  # 20 days of 1h bars
    # Vol targeting
    use_vol_sizing: bool = True
    vol_window_bars: int = 168  # 7 days of 1h bars
    target_annual_vol: float = 0.20  # aim for 20% annualized volatility
    bars_per_year: int = 8760  # 1h bars = 24*365


class TSMOM(EMACross):
    def __init__(self, config: TSMOMConfig):
        super().__init__(config)
        self._closes: deque[float] = deque(maxlen=config.lookback_bars + 1)
        self._log_rets: deque[float] = deque(maxlen=config.vol_window_bars + 1)
        self._prev_close: float | None = None

    def on_start(self) -> None:
        # No EMA indicators — TSMOM needs no warmup from them. Subscribe + calendar.
        self.subscribe_bars(self.config.bar_type)
        self._load_news_calendar()

    # ------------------------------------------------------------- signal
    def _trailing_return(self) -> float | None:
        if len(self._closes) <= self.config.lookback_bars:
            return None
        base = self._closes[0]
        if base <= 0:
            return None
        return self._closes[-1] / base - 1.0

    # ------------------------------------------------------------- vol
    def _realized_vol(self) -> float | None:
        """Annualized realized volatility from recent log returns."""
        if len(self._log_rets) < min(20, self.config.vol_window_bars):
            return None
        rets = list(self._log_rets)
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
        per_bar = math.sqrt(var)
        return per_bar * math.sqrt(self.config.bars_per_year)

    # ------------------------------------------------------------- sizing
    def _trade_qty(self, price: float) -> Decimal | None:
        if not self.config.use_vol_sizing:
            return super()._trade_qty(price)

        instrument = self.cache.instrument(self.config.instrument_id)
        equity = self._equity()
        if equity <= 0 or price <= 0:
            return None

        realized = self._realized_vol()
        if realized is None or realized <= 1e-9:
            return None  # not enough history — do not size blind

        # Scale: target 20% vol -> 1.0x size; 40% vol -> 0.5x; 10% vol -> 2x (capped)
        scale = self.config.target_annual_vol / realized
        notional = equity * min(scale, self.config.max_notional_frac)

        qty_f = notional / price
        qty = instrument.make_qty(Decimal(str(qty_f)))
        if qty <= 0:
            return None
        if float(qty) * price < 10.0:
            return None
        return qty

    # ------------------------------------------------------------- main
    def on_bar(self, bar: Bar) -> None:
        # Track history for signal + vol (before anything can skip it)
        close = float(bar.close)
        self._closes.append(close)
        if self._prev_close is not None and self._prev_close > 0:
            self._log_rets.append(math.log(close / self._prev_close))
        self._prev_close = close

        # --- shared guardrails (identical to parent) ---
        if self._apply_news_blackout(bar):
            return
        self._update_daily_state(bar)
        if self._halted_today:
            if not self.portfolio.is_flat(self.config.instrument_id):
                self.close_all_positions(self.config.instrument_id)
            return
        if self._check_stop_loss_and_take_profit(bar):
            return

        # --- TSMOM signal: sign of trailing return ---
        trailing = self._trailing_return()
        if trailing is None:
            return
        side = "LONG" if trailing > 0 else "SHORT"
        instrument = self.config.instrument_id
        price = close

        if side == "LONG":
            if self.portfolio.is_net_short(instrument):
                self.close_all_positions(instrument)
                self.buy(price)
            elif self.portfolio.is_flat(instrument):
                self.buy(price)
        else:  # SHORT
            if self.portfolio.is_net_long(instrument):
                self.close_all_positions(instrument)
                if self.config.allow_short:
                    self.sell(price)
            elif self.config.allow_short and self.portfolio.is_flat(instrument):
                self.sell(price)
