"""
Time Machine — replay a trade before risking a dollar
======================================================
Idea borrowed from Jakeschincariol/day-trader-skill (/trader-timemachine),
adapted to this SMC engine:

1. monte_carlo()       Simulates the next `horizon` bars thousands of times by
                       resampling this symbol's own recent bars (close, high
                       and low moves relative to the previous close). Counts how
                       often the TP is hit first, the SL first, or neither.
                       If one bar touches both, the SL is counted (conservative).

2. historical_replay() Walks back through the candles, re-runs the full SMC
                       decision engine at every past bar, and checks how each
                       past signal in the same direction actually resolved.

Both are reality checks built on recent data, not predictions.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class OddsResult:
    sims: int
    horizon: int
    tp_first: float
    sl_first: float
    still_open: float
    rr: float
    expected_r: float          # TP% * RR - SL% + open% * mean R at horizon end
    seed: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReplayResult:
    fired: int
    wins: int
    losses: int
    open: int
    win_rate: float            # wins / (wins + losses)
    expected_r: float          # mean R per fired signal (open counted at close R)
    samples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["samples"] = d["samples"][-10:]
        return d


def _bar_moves(candles: list[dict], lookback: int) -> list[tuple[float, float, float]]:
    """(close, high, low) of each bar relative to the previous close."""
    moves = []
    seg = candles[-(lookback + 1):]
    for prev, cur in zip(seg, seg[1:]):
        pc = prev["close"]
        if pc <= 0:
            continue
        moves.append((cur["close"] / pc - 1.0, cur["high"] / pc - 1.0, cur["low"] / pc - 1.0))
    return moves


def _resolve(direction: str, high: float, low: float, sl: float, tp: float) -> Optional[str]:
    if direction == "BUY":
        if low <= sl:
            return "sl"
        if high >= tp:
            return "tp"
    else:
        if high >= sl:
            return "sl"
        if low <= tp:
            return "tp"
    return None


def monte_carlo(candles: list[dict], direction: str, entry: float, sl: float, tp: float,
                horizon: int = 48, sims: int = 5000, lookback: int = 300,
                seed: Optional[int] = None) -> OddsResult:
    direction = direction.upper()
    risk = abs(entry - sl)
    rr = abs(tp - entry) / risk if risk > 0 else 0.0
    moves = _bar_moves(candles, lookback)
    if risk <= 0 or not moves:
        return OddsResult(sims, horizon, 0, 0, 1, rr, 0, seed)

    rnd = random.Random(seed)
    tp_n = sl_n = 0
    open_r_sum = 0.0
    sign = 1 if direction == "BUY" else -1
    for _ in range(sims):
        p = entry
        outcome = None
        for _ in range(horizon):
            c, h, l = moves[rnd.randrange(len(moves))]
            outcome = _resolve(direction, p * (1 + h), p * (1 + l), sl, tp)
            if outcome:
                break
            p = p * (1 + c)
        if outcome == "tp":
            tp_n += 1
        elif outcome == "sl":
            sl_n += 1
        else:
            open_r_sum += sign * (p - entry) / risk
    open_n = sims - tp_n - sl_n
    tp_p, sl_p, op_p = tp_n / sims, sl_n / sims, open_n / sims
    exp_r = tp_p * rr - sl_p + (open_r_sum / sims)
    return OddsResult(sims, horizon, round(tp_p, 4), round(sl_p, 4), round(op_p, 4),
                      round(rr, 2), round(exp_r, 3), seed)


def forward_outcome(candles: list[dict], start: int, direction: str, entry: float,
                    sl: float, tp: float, horizon: int) -> tuple[str, float]:
    """Resolve a trade on real bars after index `start`. Returns (outcome, R)."""
    risk = abs(entry - sl) or 1e-12
    rr = abs(tp - entry) / risk
    end = min(len(candles), start + 1 + horizon)
    last = entry
    for c in candles[start + 1:end]:
        o = _resolve(direction, c["high"], c["low"], sl, tp)
        if o == "tp":
            return "win", rr
        if o == "sl":
            return "loss", -1.0
        last = c["close"]
    sign = 1 if direction == "BUY" else -1
    return "open", sign * (last - entry) / risk


def historical_replay(candles: list[dict], symbol: str, direction: Optional[str] = None,
                      horizon: int = 48, window: int = 150, step: int = 1,
                      min_rr: float = 1.8, min_confidence: float = 0.45,
                      max_samples: int = 400) -> ReplayResult:
    """Re-run the SMC decision engine on past windows and score each signal."""
    from market_structure import analyze_market_structure
    from liquidity import analyze_liquidity
    from displacement import analyze_displacement, analyze_retracement
    from amd_phases import analyze_amd
    from decision_engine import decide, Signal

    fired = wins = losses = opens = 0
    r_sum = 0.0
    samples = []
    last_fire = -10**9
    start_i = max(window, len(candles) - horizon - max_samples)
    for i in range(start_i, len(candles) - 1, step):
        if i - last_fire < 3:          # one signal per cluster of bars
            continue
        seg = candles[i - window:i + 1]
        price = seg[-1]["close"]
        try:
            st = analyze_market_structure(seg)
            lq = analyze_liquidity(seg, price)
            dp = analyze_displacement(seg)
            rt = analyze_retracement(seg, dp.fvgs, price, dp)
            amd = analyze_amd(seg, price)
            sig = decide(seg, st, lq, dp, rt, price, amd=amd, min_rr=min_rr,
                         min_confidence=min_confidence, symbol=symbol)
        except Exception:
            continue
        if sig.signal == Signal.WAIT:
            continue
        if direction and sig.signal.value != direction.upper():
            continue
        outcome, r = forward_outcome(candles, i, sig.signal.value, price,
                                     sig.stop_loss, sig.take_profit, horizon)
        fired += 1
        last_fire = i
        r_sum += r
        wins += outcome == "win"
        losses += outcome == "loss"
        opens += outcome == "open"
        samples.append({"time": candles[i]["time"], "signal": sig.signal.value,
                        "setup": sig.setup_type, "outcome": outcome, "r": round(r, 2)})
    decided = wins + losses
    return ReplayResult(fired, wins, losses, opens,
                        round(wins / decided, 3) if decided else 0.0,
                        round(r_sum / fired, 3) if fired else 0.0, samples)


def verdict(odds: OddsResult, replay: Optional[ReplayResult], min_samples: int = 5) -> str:
    parts = [f"MC: TP {odds.tp_first:.0%} / SL {odds.sl_first:.0%} / open {odds.still_open:.0%}"
             f" → {odds.expected_r:+.2f}R"]
    if replay and replay.fired:
        parts.append(f"History: {replay.fired} fired, {replay.wins}W/{replay.losses}L"
                     f" → {replay.expected_r:+.2f}R" +
                     ("" if replay.fired >= min_samples else " (small sample)"))
    return " | ".join(parts)
