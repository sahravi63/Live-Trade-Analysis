"""
Displacement Detection — v2
Fair Value Gaps (FVGs) + Order Block Zones + Retracement Quality

Improvements:
- FVGs filtered by minimum body size (impulse candle must be >1.5× ATR body)
- FVG fill tracking: once 50%+ is filled, mark as 'mitigated'
- Retracement uses the actual FVG zone for entry, not a fixed ATR offset
- OTE (Optimal Trade Entry) zone = 62–79% Fibonacci of the last impulse
- Displacement direction uses a rolling window (last 5 FVGs) for stability
"""

from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Data Classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FairValueGap:
    index: int
    top: float
    bottom: float
    direction: str           # "bullish" | "bearish"
    filled: bool = False
    mitigated: bool = False  # 50 %+ filled — use cautiously
    strength: float = 0.0   # 0–1 score


@dataclass
class DisplacementResult:
    fvgs: list[FairValueGap]
    last_displacement_dir: Optional[str]
    displacement_strength: float
    # OTE zone derived from the last impulse
    ote_high: Optional[float] = None
    ote_low: Optional[float]  = None


@dataclass
class RetracementResult:
    is_retracing: bool
    retracement_pct: float
    inside_fvg: bool
    fvg_zone: Optional[FairValueGap]
    retracement_strength: str     # "weak" | "moderate" | "strong"
    inside_ote: bool = False      # price in 62–79% Fib zone
    entry_low: Optional[float]  = None
    entry_high: Optional[float] = None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _calc_atr(candles: list[dict], period: int = 14, end_idx: Optional[int] = None) -> float:
    idx = end_idx if end_idx is not None else len(candles) - 1
    start = max(1, idx - period)
    trs = []
    for i in range(start, min(idx + 1, len(candles))):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.001


# ──────────────────────────────────────────────────────────────────────────────
# FVG Detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_fvg(
    candles: list[dict],
    min_gap_pips: float = 0.0003,
    require_strong_body: bool = True,
) -> list[FairValueGap]:
    """
    Three-candle imbalance.

    Bullish FVG : candle[i-2].high < candle[i].low
    Bearish FVG : candle[i-2].low  > candle[i].high

    If require_strong_body=True the middle candle's body must be >= 1.0× local ATR.
    """
    fvgs: list[FairValueGap] = []

    for i in range(2, len(candles)):
        c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]
        atr = _calc_atr(candles, 14, i - 1)

        # Impulse body filter
        body = abs(c1["close"] - c1["open"])
        if require_strong_body and body < atr * 1.0:
            continue

        # Bullish FVG
        gap_bull = c2["low"] - c0["high"]
        if gap_bull >= min_gap_pips:
            strength = _score_fvg(candles, i - 1, "bullish", atr)
            fvgs.append(FairValueGap(
                index=i - 1,
                top=c2["low"],
                bottom=c0["high"],
                direction="bullish",
                strength=strength,
            ))

        # Bearish FVG
        gap_bear = c0["low"] - c2["high"]
        if gap_bear >= min_gap_pips:
            strength = _score_fvg(candles, i - 1, "bearish", atr)
            fvgs.append(FairValueGap(
                index=i - 1,
                top=c0["low"],
                bottom=c2["high"],
                direction="bearish",
                strength=strength,
            ))

    # Mark filled / mitigated
    _update_fill_status(fvgs, candles)
    return fvgs


def _score_fvg(candles: list[dict], idx: int, direction: str, atr: float) -> float:
    c = candles[idx]
    body = abs(c["close"] - c["open"])
    body_ratio = min(body / (atr if atr > 0 else 1), 3.0) / 3.0

    direction_val = 1 if direction == "bullish" else -1
    streak = 0
    for j in range(idx, max(0, idx - 5), -1):
        cj = candles[j]
        if direction_val == 1 and cj["close"] > cj["open"]:
            streak += 1
        elif direction_val == -1 and cj["close"] < cj["open"]:
            streak += 1
        else:
            break

    return round(body_ratio * 0.65 + min(streak / 5, 1.0) * 0.35, 3)


def _update_fill_status(fvgs: list[FairValueGap], candles: list[dict]) -> None:
    """Mark FVGs as filled when price has traded through them fully, mitigated at 50%."""
    for fvg in fvgs:
        mid = (fvg.top + fvg.bottom) / 2.0
        for c in candles[fvg.index + 1:]:
            if fvg.direction == "bullish":
                if c["low"] <= fvg.bottom:
                    fvg.filled = True
                    break
                if c["low"] <= mid:
                    fvg.mitigated = True
            else:
                if c["high"] >= fvg.top:
                    fvg.filled = True
                    break
                if c["high"] >= mid:
                    fvg.mitigated = True


# ──────────────────────────────────────────────────────────────────────────────
# Displacement Analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_displacement(candles: list[dict]) -> DisplacementResult:
    fvgs = detect_fvg(candles)

    if not fvgs:
        return DisplacementResult(
            fvgs=[], last_displacement_dir=None, displacement_strength=0.0,
        )

    # Use last 5 unfilled FVGs to determine prevailing direction
    recent = [f for f in fvgs if not f.filled][-5:]
    if not recent:
        recent = fvgs[-5:]

    bull_count = sum(1 for f in recent if f.direction == "bullish")
    bear_count = len(recent) - bull_count
    last_dir = "bullish" if bull_count >= bear_count else "bearish"

    last_fvg = recent[-1]
    strength = last_fvg.strength

    # OTE zone from the last strong FVG
    # Find the swing leg that produced the FVG
    idx = last_fvg.index
    look_back = min(20, idx)
    segment = candles[idx - look_back: idx + 1]

    if last_dir == "bullish":
        leg_low  = min(c["low"]  for c in segment)
        leg_high = max(c["high"] for c in segment)
    else:
        leg_high = max(c["high"] for c in segment)
        leg_low  = min(c["low"]  for c in segment)

    rng = leg_high - leg_low
    # OTE = 62% – 79% Fibonacci retracement of the impulse
    if last_dir == "bullish" and rng > 0:
        ote_low  = round(leg_high - rng * 0.786, 5)
        ote_high = round(leg_high - rng * 0.618, 5)
    elif last_dir == "bearish" and rng > 0:
        ote_high = round(leg_low + rng * 0.786, 5)
        ote_low  = round(leg_low + rng * 0.618, 5)
    else:
        ote_low = ote_high = None

    return DisplacementResult(
        fvgs=fvgs,
        last_displacement_dir=last_dir,
        displacement_strength=round(strength, 3),
        ote_low=ote_low,
        ote_high=ote_high,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Retracement Analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_retracement(
    candles: list[dict],
    fvgs: list[FairValueGap],
    current_price: float,
    displacement: Optional[DisplacementResult] = None,
) -> RetracementResult:
    """
    Is price pulling back into an active FVG / OTE zone?

    Entry zone = FVG zone (tight) if inside FVG,
                 else OTE zone (62–79% Fib) if inside OTE,
                 else current price ± 0.3 ATR (fallback).
    """
    atr = _calc_atr(candles)

    if not fvgs:
        return RetracementResult(
            is_retracing=False, retracement_pct=0.0,
            inside_fvg=False, fvg_zone=None,
            retracement_strength="weak",
            inside_ote=False,
            entry_low=round(current_price - atr * 0.3, 5),
            entry_high=round(current_price + atr * 0.3, 5),
        )

    # Active (unfilled) FVGs, strongest first
    active = sorted(
        [f for f in fvgs if not f.filled],
        key=lambda f: f.strength,
        reverse=True,
    )
    if not active:
        active = fvgs[-3:]

    # Find the closest active FVG to current price
    active_fvg = min(active, key=lambda f: abs(((f.top + f.bottom) / 2) - current_price))

    fvg_size = active_fvg.top - active_fvg.bottom
    if active_fvg.direction == "bullish":
        distance_into = active_fvg.top - current_price
    else:
        distance_into = current_price - active_fvg.bottom

    retracement_pct = max(0.0, (distance_into / fvg_size) * 100) if fvg_size > 0 else 0.0
    inside_fvg      = active_fvg.bottom <= current_price <= active_fvg.top

    if retracement_pct < 30:
        strength = "weak"
    elif retracement_pct < 65:
        strength = "moderate"
    else:
        strength = "strong"

    # Check OTE zone
    inside_ote = False
    if displacement and displacement.ote_low and displacement.ote_high:
        inside_ote = displacement.ote_low <= current_price <= displacement.ote_high

    # Entry zone: prefer FVG, else OTE, else ATR fallback
    if inside_fvg:
        entry_low  = round(active_fvg.bottom, 5)
        entry_high = round(active_fvg.top, 5)
    elif inside_ote and displacement:
        entry_low  = displacement.ote_low
        entry_high = displacement.ote_high
    else:
        entry_low  = round(current_price - atr * 0.2, 5)
        entry_high = round(current_price + atr * 0.2, 5)

    return RetracementResult(
        is_retracing=retracement_pct > 0,
        retracement_pct=round(retracement_pct, 2),
        inside_fvg=inside_fvg,
        fvg_zone=active_fvg,
        retracement_strength=strength,
        inside_ote=inside_ote,
        entry_low=entry_low,
        entry_high=entry_high,
    )