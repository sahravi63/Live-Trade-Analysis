"""
Paper broker — simulated fills, persisted to disk.

Opens at the bot's execution price, then on every scan walks the new candles
since entry: SL hit -> loss, TP hit -> win (if one bar touches both, the SL is
assumed first), or closes at market after `max_hold_bars`. P&L uses the same
per-symbol pip values as the risk manager.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

from risk_manager import _pip_size, _get_pip_value


def _ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Position:
    id: str
    symbol: str
    timeframe: str
    direction: str
    entry: float
    sl: float
    tp: float
    lots: float
    risk_amount: float
    opened_at: str
    entry_bar_time: str
    setup: str = ""
    confidence: float = 0.0
    mode: str = "paper"              # paper | mt5-demo
    ticket: Optional[int] = None     # MT5 ticket when mode == mt5-demo
    bars_held: int = 0
    status: str = "open"             # open | closed
    exit_price: Optional[float] = None
    closed_at: Optional[str] = None
    exit_reason: str = ""
    pnl: float = 0.0
    r_multiple: float = 0.0
    mark_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    level_updated_after: Optional[str] = None
    timemachine: dict = field(default_factory=dict)


class PaperBroker:
    def __init__(self, path: str, starting_balance: float = 10_000.0, max_hold_bars: int = 48):
        self.path = path
        self.max_hold_bars = max_hold_bars
        self.starting_balance = starting_balance
        self.cash_balance = starting_balance
        self.funding_events: list[dict] = []
        self.positions: list[Position] = []
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.starting_balance = d.get("starting_balance", self.starting_balance)
            self.funding_events = d.get("funding_events", [])
            self.positions = [Position(**p) for p in d.get("positions", [])]
            self.cash_balance = d.get(
                "cash_balance",
                self.starting_balance + sum(p.pnl for p in self.closed_positions()),
            )
        except (OSError, ValueError, TypeError) as e:
            print(f"[paper] could not read {self.path}: {e} — starting fresh")

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"starting_balance": self.starting_balance,
                       "cash_balance": self.cash_balance,
                       "funding_events": self.funding_events,
                       "positions": [asdict(p) for p in self.positions]}, fh, indent=1)
        os.replace(tmp, self.path)

    # ── queries ──────────────────────────────────────────────────────────────
    def open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        return [p for p in self.positions if p.status == "open"
                and (symbol is None or p.symbol == symbol)]

    def closed_positions(self) -> list[Position]:
        return [p for p in self.positions if p.status == "closed"]

    def balance(self) -> float:
        return round(self.cash_balance, 2)

    def floating_pnl(self) -> float:
        return round(sum(p.unrealized_pnl for p in self.open_positions()), 2)

    def equity(self) -> float:
        return round(self.balance() + self.floating_pnl(), 2)

    def deposit(self, amount: float) -> dict:
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Deposit amount must be at least $0.01.")
        amount = round(amount, 2)
        if amount <= 0:
            raise ValueError("Deposit amount must be at least $0.01.")
        self.cash_balance = round(self.cash_balance + amount, 2)
        event = {"type": "deposit", "amount": round(amount, 2),
                 "balance": self.balance(), "ts": datetime.now(timezone.utc).isoformat()}
        self.funding_events.append(event)
        self.save()
        return event

    def withdraw(self, amount: float) -> dict:
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Withdrawal amount must be at least $0.01.")
        amount = round(amount, 2)
        if amount <= 0:
            raise ValueError("Withdrawal amount must be at least $0.01.")
        available = self.balance()
        if amount > available:
            raise ValueError(f"Withdrawal exceeds available balance (${available:.2f}).")
        self.cash_balance = round(self.cash_balance - amount, 2)
        event = {"type": "withdrawal", "amount": round(amount, 2),
                 "balance": self.balance(), "ts": datetime.now(timezone.utc).isoformat()}
        self.funding_events.append(event)
        self.save()
        return event

    def mark_to_market(self, symbol: str, timeframe: str, price: float) -> None:
        for p in self.open_positions(symbol):
            if p.timeframe != timeframe or p.mode != "paper":
                continue
            p.mark_price = price
            p.unrealized_pnl = self.pnl_for(p, price)

    def stats(self) -> dict:
        closed = self.closed_positions()
        wins = [p for p in closed if p.pnl > 0]
        losses = [p for p in closed if p.pnl <= 0]
        gross_w = sum(p.pnl for p in wins)
        gross_l = -sum(p.pnl for p in losses)
        return {
            "starting_balance": self.starting_balance,
            "balance": self.balance(),
            "equity": self.equity(),
            "floating_pnl": self.floating_pnl(),
            "net_pnl": round(gross_w - gross_l, 2),
            "trades": len(closed),
            "open": len(self.open_positions()),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed), 3) if closed else 0.0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
            "avg_r": round(sum(p.r_multiple for p in closed) / len(closed), 2) if closed else 0.0,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────
    def pnl_for(self, p: Position, exit_price: float) -> float:
        sign = 1 if p.direction == "BUY" else -1
        pips = sign * (exit_price - p.entry) / _pip_size(p.symbol)
        return round(pips * _get_pip_value(p.symbol) * p.lots, 2)

    def open(self, symbol: str, timeframe: str, direction: str, entry: float, sl: float,
             tp: float, lots: float, risk_amount: float, entry_bar_time: str,
             setup: str = "", confidence: float = 0.0, timemachine: Optional[dict] = None,
             mode: str = "paper", ticket: Optional[int] = None) -> Position:
        p = Position(id="P" + uuid.uuid4().hex[:8], symbol=symbol, timeframe=timeframe,
                     direction=direction, entry=entry, sl=sl, tp=tp, lots=lots,
                     risk_amount=risk_amount,
                     opened_at=datetime.now(timezone.utc).isoformat(),
                     entry_bar_time=entry_bar_time, setup=setup, confidence=confidence,
                     mode=mode, ticket=ticket, timemachine=timemachine or {})
        self.positions.append(p)
        self.save()
        return p

    def close(self, p: Position, price: float, reason: str) -> Position:
        if p.status != "open":
            raise ValueError(f"Position {p.id} is already closed.")
        p.status = "closed"
        p.exit_price = round(price, 5)
        p.closed_at = datetime.now(timezone.utc).isoformat()
        p.exit_reason = reason
        p.pnl = self.pnl_for(p, price)
        self.cash_balance = round(self.cash_balance + p.pnl, 2)
        p.mark_price = p.exit_price
        p.unrealized_pnl = 0.0
        risk_dist = abs(p.entry - p.sl) or 1e-12
        sign = 1 if p.direction == "BUY" else -1
        p.r_multiple = round(sign * (price - p.entry) / risk_dist, 2)
        self.save()
        return p

    def update_position(
        self,
        p: Position,
        stop_loss: float,
        take_profit: float,
        lots: float,
        bar_time: str,
        max_risk_amount: float,
        spread_buffer_pips: float = 1.0,
    ) -> Position:
        if p.status != "open":
            raise ValueError(f"Position {p.id} is not open.")
        if not all(math.isfinite(value) and value > 0 for value in (
            stop_loss, take_profit, lots, max_risk_amount,
        )):
            raise ValueError("SL, TP, lots, and risk limit must be positive finite numbers.")
        if not math.isclose(lots, round(lots, 2), abs_tol=1e-9):
            raise ValueError("Lot size must use 0.01 lot increments.")
        if lots < 0.01:
            raise ValueError("Minimum lot size is 0.01.")
        try:
            _ts(bar_time)
        except (TypeError, ValueError) as e:
            raise ValueError("Invalid candle time for the order update.") from e

        mark = p.mark_price if p.mark_price is not None else p.entry
        stop_loss = round(stop_loss, 5)
        take_profit = round(take_profit, 5)
        if p.direction == "BUY":
            if stop_loss >= mark or stop_loss < p.sl:
                raise ValueError("BUY stop loss must stay below market and cannot widen risk.")
            if take_profit <= max(mark, p.entry):
                raise ValueError("BUY take profit must stay above market and entry.")
        else:
            if stop_loss <= mark or stop_loss > p.sl:
                raise ValueError("SELL stop loss must stay above market and cannot widen risk.")
            if take_profit >= min(mark, p.entry):
                raise ValueError("SELL take profit must stay below market and entry.")

        risk_pips = abs(p.entry - stop_loss) / _pip_size(p.symbol) + spread_buffer_pips
        risk_amount = round(risk_pips * _get_pip_value(p.symbol) * lots, 2)
        if risk_amount > max_risk_amount:
            raise ValueError(
                f"Updated lot size risks ${risk_amount:.2f}, above the "
                f"${max_risk_amount:.2f} per-trade risk limit."
            )

        p.sl = stop_loss
        p.tp = take_profit
        p.lots = round(lots, 2)
        p.risk_amount = risk_amount
        p.level_updated_after = bar_time
        self.save()
        return p

    def update(self, symbol: str, timeframe: str, candles: list[dict]) -> list[Position]:
        """Check open paper positions on this symbol/timeframe against new candles.
        Returns the positions closed during this update."""
        closed = []
        if candles:
            self.mark_to_market(symbol, timeframe, candles[-1]["close"])
        for p in self.open_positions(symbol):
            if p.timeframe != timeframe or p.mode != "paper":
                continue
            start = _ts(p.entry_bar_time)
            new_bars = [c for c in candles if _ts(c["time"]) > start]
            p.bars_held = len(new_bars)
            level_start = _ts(p.level_updated_after) if p.level_updated_after else start
            for c in (bar for bar in new_bars if _ts(bar["time"]) >= level_start):
                hit_sl = c["low"] <= p.sl if p.direction == "BUY" else c["high"] >= p.sl
                hit_tp = c["high"] >= p.tp if p.direction == "BUY" else c["low"] <= p.tp
                if hit_sl:
                    closed.append(self.close(p, p.sl, "stop loss"))
                    break
                if hit_tp:
                    closed.append(self.close(p, p.tp, "take profit"))
                    break
            else:
                if new_bars and p.bars_held >= self.max_hold_bars:
                    closed.append(self.close(p, new_bars[-1]["close"],
                                             f"time exit ({self.max_hold_bars} bars)"))
        if not closed:
            self.save()
        return closed

    def reset(self, starting_balance: Optional[float] = None) -> None:
        self.positions = []
        if starting_balance:
            self.starting_balance = starting_balance
        self.cash_balance = self.starting_balance
        self.funding_events = []
        self.save()
