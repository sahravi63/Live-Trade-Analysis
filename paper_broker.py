"""
Paper broker — simulated fills, persisted to disk.

Opens at the bot's execution price, then on every scan walks the new candles
since entry: SL hit -> loss, TP hit -> win (if one bar touches both, the SL is
assumed first), or closes at market after `max_hold_bars`. P&L uses the same
per-symbol pip values as the risk manager.
"""
from __future__ import annotations

import json
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
    timemachine: dict = field(default_factory=dict)


class PaperBroker:
    def __init__(self, path: str, starting_balance: float = 10_000.0, max_hold_bars: int = 48):
        self.path = path
        self.max_hold_bars = max_hold_bars
        self.starting_balance = starting_balance
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
            self.positions = [Position(**p) for p in d.get("positions", [])]
        except (OSError, ValueError, TypeError) as e:
            print(f"[paper] could not read {self.path}: {e} — starting fresh")

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"starting_balance": self.starting_balance,
                       "positions": [asdict(p) for p in self.positions]}, fh, indent=1)
        os.replace(tmp, self.path)

    # ── queries ──────────────────────────────────────────────────────────────
    def open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        return [p for p in self.positions if p.status == "open"
                and (symbol is None or p.symbol == symbol)]

    def closed_positions(self) -> list[Position]:
        return [p for p in self.positions if p.status == "closed"]

    def equity(self) -> float:
        return round(self.starting_balance + sum(p.pnl for p in self.closed_positions()), 2)

    def stats(self) -> dict:
        closed = self.closed_positions()
        wins = [p for p in closed if p.pnl > 0]
        losses = [p for p in closed if p.pnl <= 0]
        gross_w = sum(p.pnl for p in wins)
        gross_l = -sum(p.pnl for p in losses)
        return {
            "starting_balance": self.starting_balance,
            "equity": self.equity(),
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
        p.status = "closed"
        p.exit_price = round(price, 5)
        p.closed_at = datetime.now(timezone.utc).isoformat()
        p.exit_reason = reason
        p.pnl = self.pnl_for(p, price)
        risk_dist = abs(p.entry - p.sl) or 1e-12
        sign = 1 if p.direction == "BUY" else -1
        p.r_multiple = round(sign * (price - p.entry) / risk_dist, 2)
        self.save()
        return p

    def update(self, symbol: str, timeframe: str, candles: list[dict]) -> list[Position]:
        """Check open paper positions on this symbol/timeframe against new candles.
        Returns the positions closed during this update."""
        closed = []
        for p in self.open_positions(symbol):
            if p.timeframe != timeframe or p.mode != "paper":
                continue
            start = _ts(p.entry_bar_time)
            new_bars = [c for c in candles if _ts(c["time"]) > start]
            p.bars_held = len(new_bars)
            for c in new_bars:
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
        self.save()
