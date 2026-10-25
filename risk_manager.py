"""
Scalping Risk Manager — v1
===========================
Handles ALL money-management concerns that a "signal only" engine ignores:

  1. Position sizing      — risk a fixed % of account per trade (default 0.5%)
  2. Daily loss limit     — halt trading after X% drawdown in one session
  3. Trade cooldown       — minimum gap (seconds) between entries to avoid overtrading
  4. Max concurrent trades — soft cap on open positions
  5. Consecutive loss guard — pause after N back-to-back losses
  6. Spread-adjusted SL   — widen SL by spread so broker can't sweep you instantly

Usage:
    rm = RiskManager(account_balance=10_000)
    sizing = rm.size_trade(entry=1.08500, sl=1.08350, symbol="EURUSD")
    if sizing.allowed:
        rm.record_trade_open(trade_id="T1", entry=1.08500)
    # ... later ...
    rm.record_trade_close(trade_id="T1", pnl=-15.0)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import time as _time


# ── Per-symbol pip value (USD per standard lot, approximate) ──────────────────
# Forex: 1 pip = 0.0001, except JPY pairs where 1 pip = 0.01.
# Gold (XAUUSD): price unit = $0.01, so 1 "pip" = $1 per lot.
# Values are approximate; for production use live tick values from the broker.
_PIP_VALUE_TABLE: dict[str, float] = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "AUDUSD": 10.0,
    "NZDUSD": 10.0,
    "USDCHF": 9.2,   # ~10 / USDCHF rate; update live
    "USDCAD": 7.5,   # ~10 / USDCAD rate; update live
    "USDJPY": 9.3,   # 1000 / USDJPY rate (pip=0.01, lot=100k); update live
    "GBPJPY": 9.3,
    "EURJPY": 9.3,
    "EURGBP": 13.0,  # ~10 × GBPUSD rate; update live
    "XAUUSD": 1.0,   # 1 pip = $0.01 × 100 oz = $1
}

# SL range overrides in pips (0.01 units for gold = $0.50 .. $15.00)
_SL_BOUNDS_OVERRIDE: dict[str, tuple[float, float]] = {
    "XAUUSD": (50.0, 1500.0),
}


def _pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "XAU" in s or "GOLD" in s or "JPY" in s:
        return 0.01
    return 0.0001


def _get_pip_value(symbol: str) -> float:
    """USD value of one pip for one standard lot (approximate, static table)."""
    return _PIP_VALUE_TABLE.get(symbol.upper(), 10.0)


# ──────────────────────────────────────────────────────────────────────────────
# Data Classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SizingResult:
    allowed: bool
    lots: float                      # standard lots
    units: int                       # units (lots × 100 000 for FX)
    risk_amount: float               # $ at risk
    risk_pct: float                  # % of account at risk
    sl_pips: float                   # distance to SL in pips
    sl_adjusted: float               # SL price after spread adjustment
    reason: str                      # why allowed/blocked


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    open_trades: dict = field(default_factory=dict)   # trade_id → entry price
    last_entry_ts: float = 0.0                        # unix timestamp
    trade_log: list = field(default_factory=list)     # list of closed trade dicts
    halted: bool = False
    halt_reason: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Risk Manager
# ──────────────────────────────────────────────────────────────────────────────

class RiskManager:
    """
    Stateful risk manager for a single trading session.
    Re-instantiate (or call reset()) at the start of each trading day.
    """

    def __init__(
        self,
        account_balance: float = 10_000.0,
        risk_pct_per_trade: float = 0.005,      # 0.5% per trade
        max_daily_loss_pct: float = 0.02,        # 2% daily drawdown halt
        min_cooldown_seconds: float = 60.0,      # 1 min between trades (scalp)
        max_open_trades: int = 2,
        max_consecutive_losses: int = 3,
        spread_buffer_pips: float = 1.0,         # extra pips added to SL
        min_sl_pips: float = 3.0,               # never put SL closer than 3 pips
        max_sl_pips: float = 20.0,              # scalp SL should never exceed 20p
    ):
        self.balance               = account_balance
        self.risk_pct              = risk_pct_per_trade
        self.max_daily_loss_pct    = max_daily_loss_pct
        self.min_cooldown          = min_cooldown_seconds
        self.max_open              = max_open_trades
        self.max_consec_losses     = max_consecutive_losses
        # pip_value is now looked up per-symbol via _get_pip_value()
        self.spread_buffer_pips    = spread_buffer_pips
        self.min_sl_pips           = min_sl_pips
        self.max_sl_pips           = max_sl_pips

        self.state = RiskState()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pips(self, price1: float, price2: float, symbol: str = "EURUSD") -> float:
        """Return pip distance (JPY and gold pip = 0.01, other FX = 0.0001)."""
        return round(abs(price1 - price2) / _pip_size(symbol), 1)

    def _sl_bounds(self, symbol: str) -> tuple[float, float]:
        """Per-symbol SL pip range. Gold moves in dollars, so its pip range
        (0.01 units) must be far wider than an FX pair's."""
        lo, hi = _SL_BOUNDS_OVERRIDE.get(symbol.upper(), (self.min_sl_pips, self.max_sl_pips))
        return lo, hi

    def _dollar_risk(self) -> float:
        return self.balance * self.risk_pct

    def _current_ts(self) -> float:
        return _time.time()

    # ── Hard Checks ───────────────────────────────────────────────────────────

    def _check_halt(self) -> Optional[str]:
        if self.state.halted:
            return f"Trading halted: {self.state.halt_reason}"
        return None

    def _check_daily_loss(self) -> Optional[str]:
        loss_pct = abs(min(self.state.daily_pnl, 0)) / self.balance
        if loss_pct >= self.max_daily_loss_pct:
            self.state.halted = True
            self.state.halt_reason = f"Daily loss limit reached ({loss_pct:.1%})"
            return self.state.halt_reason
        return None

    def _check_cooldown(self) -> Optional[str]:
        elapsed = self._current_ts() - self.state.last_entry_ts
        if self.state.last_entry_ts > 0 and elapsed < self.min_cooldown:
            remaining = int(self.min_cooldown - elapsed)
            return f"Cooldown active — wait {remaining}s before next entry"
        return None

    def _check_open_trades(self) -> Optional[str]:
        if len(self.state.open_trades) >= self.max_open:
            return f"Max open trades ({self.max_open}) reached"
        return None

    def _check_consec_losses(self) -> Optional[str]:
        if self.state.consecutive_losses >= self.max_consec_losses:
            return (
                f"{self.state.consecutive_losses} consecutive losses — "
                "manual review required before resuming"
            )
        return None

    # ── Main: Size a Trade ────────────────────────────────────────────────────

    def size_trade(
        self,
        entry: float,
        sl: float,
        symbol: str = "EURUSD",
        direction: str = "BUY",
    ) -> SizingResult:
        """
        Calculate lot size and check all risk rules before allowing entry.
        """
        # Run hard gates
        for check in [
            self._check_halt,
            self._check_daily_loss,
            self._check_cooldown,
            self._check_open_trades,
            self._check_consec_losses,
        ]:
            block = check()
            if block:
                return SizingResult(
                    allowed=False, lots=0, units=0,
                    risk_amount=0, risk_pct=0,
                    sl_pips=0, sl_adjusted=sl,
                    reason=block,
                )

        sl_pips = self._pips(entry, sl, symbol)
        min_sl, max_sl = self._sl_bounds(symbol)

        # Clamp SL range
        if sl_pips < min_sl:
            return SizingResult(
                allowed=False, lots=0, units=0,
                risk_amount=0, risk_pct=self.risk_pct,
                sl_pips=sl_pips, sl_adjusted=sl,
                reason=f"SL too tight ({sl_pips:.1f} pips < min {min_sl})",
            )
        if sl_pips > max_sl:
            return SizingResult(
                allowed=False, lots=0, units=0,
                risk_amount=0, risk_pct=self.risk_pct,
                sl_pips=sl_pips, sl_adjusted=sl,
                reason=f"SL too wide ({sl_pips:.1f} pips > max {max_sl})",
            )

        # Spread-adjusted SL (so broker spread doesn't immediately trigger it)
        pip_size = _pip_size(symbol)
        if direction == "BUY":
            sl_adjusted = round(sl - self.spread_buffer_pips * pip_size, 5)
        else:
            sl_adjusted = round(sl + self.spread_buffer_pips * pip_size, 5)

        # Effective SL pips after buffer
        effective_sl_pips = sl_pips + self.spread_buffer_pips

        # Lot sizing — use per-symbol pip value (not a fixed $10 for all pairs)
        pip_value = _get_pip_value(symbol)
        dollar_risk = self._dollar_risk()
        # 1 pip = pip_value per lot
        lots = round(dollar_risk / (effective_sl_pips * pip_value), 2)
        lots = max(0.01, lots)   # minimum micro lot
        units = int(lots * 100_000)

        return SizingResult(
            allowed=True,
            lots=lots,
            units=units,
            risk_amount=round(dollar_risk, 2),
            risk_pct=self.risk_pct,
            sl_pips=sl_pips,
            sl_adjusted=sl_adjusted,
            reason="OK",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def record_trade_open(self, trade_id: str, entry: float) -> None:
        self.state.open_trades[trade_id] = entry
        self.state.last_entry_ts = self._current_ts()

    def record_trade_close(self, trade_id: str, pnl: float) -> None:
        """Call this when a trade closes. pnl in account currency."""
        self.state.open_trades.pop(trade_id, None)
        self.state.daily_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        self.state.trade_log.append({
            "trade_id": trade_id,
            "pnl": pnl,
            "ts": datetime.now(timezone.utc).isoformat(),
            "daily_pnl": round(self.state.daily_pnl, 2),
        })

    def reset(self, new_balance: Optional[float] = None) -> None:
        """Reset for a new trading day. Optionally update balance."""
        if new_balance:
            self.balance = new_balance
        self.state = RiskState()

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "balance": self.balance,
            "daily_pnl": round(self.state.daily_pnl, 2),
            "daily_pnl_pct": round(self.state.daily_pnl / self.balance * 100, 3),
            "open_trades": len(self.state.open_trades),
            "consecutive_losses": self.state.consecutive_losses,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
            "trades_today": len(self.state.trade_log),
            "daily_loss_remaining_pct": round(
                self.max_daily_loss_pct - abs(min(self.state.daily_pnl, 0)) / self.balance,
                4,
            ),
        }

    # ── Persistence (used by the bot so restarts keep the day's risk state) ──
    def to_dict(self) -> dict:
        st = self.state
        return {
            "balance": self.balance,
            "daily_pnl": st.daily_pnl,
            "consecutive_losses": st.consecutive_losses,
            "open_trades": dict(st.open_trades),
            "last_entry_ts": st.last_entry_ts,
            "trade_log": list(st.trade_log),
            "halted": st.halted,
            "halt_reason": st.halt_reason,
        }

    def load_dict(self, d: dict) -> None:
        self.balance = float(d.get("balance", self.balance))
        self.state = RiskState(
            daily_pnl=float(d.get("daily_pnl", 0.0)),
            consecutive_losses=int(d.get("consecutive_losses", 0)),
            open_trades=dict(d.get("open_trades", {})),
            last_entry_ts=float(d.get("last_entry_ts", 0.0)),
            trade_log=list(d.get("trade_log", [])),
            halted=bool(d.get("halted", False)),
            halt_reason=str(d.get("halt_reason", "")),
        )
