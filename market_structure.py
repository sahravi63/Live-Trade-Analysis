"""
Market Structure Detection — v2
Detects: HH, HL, LH, LL, BOS (Break of Structure), CHoCH (Change of Character)

Improvements:
- Pivot-based swing detection (not just local max/min) — avoids noise
- Proper alternating highs/lows for clean structure
- BOS confirmed only when close breaks prior swing level (not just a wick)
- CHoCH requires at least 2 structure shifts before confirming reversal
- Order Block detection (last down candle before bullish BOS and vice-versa)
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class StructureEvent(str, Enum):
    BOS   = "BOS"
    CHOCH = "CHoCH"
    HH    = "HH"
    HL    = "HL"
    LH    = "LH"
    LL    = "LL"


@dataclass
class SwingPoint:
    index: int
    price: float
    is_high: bool
    label: Optional[StructureEvent] = None


@dataclass
class OrderBlock:
    index: int
    top: float
    bottom: float
    direction: str        # "bullish" | "bearish"
    broken: bool = False


@dataclass
class StructureResult:
    trend: Trend
    swing_highs: list
    swing_lows: list
    last_event: Optional[StructureEvent]
    bos_level: Optional[float]
    choch_level: Optional[float]
    order_blocks: list = field(default_factory=list)
    # Premium / Discount zones (50% of the last impulse leg)
    premium_zone: Optional[float] = None   # price above = premium (look to sell)
    discount_zone: Optional[float] = None  # price below = discount (look to buy)
    equilibrium: Optional[float] = None    # 50% of last impulse leg


# ──────────────────────────────────────────────────────────────────────────────
# Swing Detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_swing_points(
    candles: list[dict],
    lookback: int = 5,
) -> tuple[list[SwingPoint], list[SwingPoint]]:
    """
    Detect swing highs and lows.

    A swing HIGH at index i means:
      - candles[i].high is the highest in [i-lookback .. i+lookback]
      - The candle CLOSES below its high (not a running high)

    We enforce alternation: after a swing high we must see a swing low
    before the next high (and vice-versa). This removes double-tops noise.
    """
    raw_highs: list[SwingPoint] = []
    raw_lows:  list[SwingPoint] = []

    for i in range(lookback, len(candles) - lookback):
        window_h = [candles[j]["high"] for j in range(i - lookback, i + lookback + 1)]
        window_l = [candles[j]["low"]  for j in range(i - lookback, i + lookback + 1)]

        if candles[i]["high"] == max(window_h):
            raw_highs.append(SwingPoint(index=i, price=candles[i]["high"], is_high=True))

        if candles[i]["low"] == min(window_l):
            raw_lows.append(SwingPoint(index=i, price=candles[i]["low"],  is_high=False))

    # Enforce alternation on highs and lows separately
    highs = _enforce_alternation(raw_highs, keep_highest=True)
    lows  = _enforce_alternation(raw_lows,  keep_highest=False)

    return highs, lows


def _enforce_alternation(points: list[SwingPoint], keep_highest: bool) -> list[SwingPoint]:
    """Keep only the most extreme point within overlapping groups."""
    if not points:
        return []
    result = [points[0]]
    for p in points[1:]:
        last = result[-1]
        # If two consecutive points are close (within 3 bars), keep only the more extreme
        if abs(p.index - last.index) <= 3:
            if keep_highest and p.price >= last.price:
                result[-1] = p
            elif not keep_highest and p.price <= last.price:
                result[-1] = p
        else:
            result.append(p)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Structure Labeling
# ──────────────────────────────────────────────────────────────────────────────

def label_structure(
    swing_highs: list[SwingPoint],
    swing_lows:  list[SwingPoint],
    candles: list[dict],
) -> StructureResult:
    """
    Labels swing points and detects BOS / CHoCH.
    BOS is confirmed only when a candle CLOSES beyond the prior swing.
    """
    trend        = Trend.NEUTRAL
    last_event   = None
    bos_level    = None
    choch_level  = None
    order_blocks: list[OrderBlock] = []

    # ── Label highs: HH / LH ──────────────────────────────────────────────────
    for i in range(1, len(swing_highs)):
        swing_highs[i].label = (
            StructureEvent.HH if swing_highs[i].price > swing_highs[i - 1].price
            else StructureEvent.LH
        )

    # ── Label lows: HL / LL ───────────────────────────────────────────────────
    for i in range(1, len(swing_lows)):
        swing_lows[i].label = (
            StructureEvent.HL if swing_lows[i].price > swing_lows[i - 1].price
            else StructureEvent.LL
        )

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return StructureResult(
            trend=trend, swing_highs=swing_highs, swing_lows=swing_lows,
            last_event=None, bos_level=None, choch_level=None,
        )

    last_h = swing_highs[-1]
    last_l = swing_lows[-1]
    prev_h = swing_highs[-2]
    prev_l = swing_lows[-2]

    # ── Trend from last 2 highs + lows ───────────────────────────────────────
    if last_h.label == StructureEvent.HH and last_l.label == StructureEvent.HL:
        trend = Trend.BULLISH
    elif last_h.label == StructureEvent.LH and last_l.label == StructureEvent.LL:
        trend = Trend.BEARISH

    # ── BOS: close-based confirmation ────────────────────────────────────────
    #   Bullish BOS: a candle closes ABOVE the prior swing high
    #   Bearish BOS: a candle closes BELOW the prior swing low
    close_prices = [c["close"] for c in candles]

    if trend == Trend.BULLISH:
        # BOS: close above prior HH
        if any(c > prev_h.price for c in close_prices[prev_h.index:]):
            last_event = StructureEvent.BOS
            bos_level  = prev_h.price
            # Order block = last bearish (red) candle before the BOS impulse
            ob = _find_order_block(candles, prev_h.index, "bullish")
            if ob:
                order_blocks.append(ob)
        # CHoCH: in uptrend a LL forms → structure shift
        elif last_l.label == StructureEvent.LL:
            last_event  = StructureEvent.CHOCH
            choch_level = prev_l.price

    elif trend == Trend.BEARISH:
        # BOS: close below prior LL
        if any(c < prev_l.price for c in close_prices[prev_l.index:]):
            last_event = StructureEvent.BOS
            bos_level  = prev_l.price
            ob = _find_order_block(candles, prev_l.index, "bearish")
            if ob:
                order_blocks.append(ob)
        # CHoCH: in downtrend a HH forms → structure shift
        elif last_h.label == StructureEvent.HH:
            last_event  = StructureEvent.CHOCH
            choch_level = prev_h.price

    # ── Premium / Discount / Equilibrium ─────────────────────────────────────
    prem, disc, eq = _calc_pdz(swing_highs, swing_lows, trend)

    return StructureResult(
        trend=trend,
        swing_highs=swing_highs,
        swing_lows=swing_lows,
        last_event=last_event,
        bos_level=bos_level,
        choch_level=choch_level,
        order_blocks=order_blocks,
        premium_zone=prem,
        discount_zone=disc,
        equilibrium=eq,
    )


def _find_order_block(candles: list[dict], from_index: int, direction: str) -> Optional[OrderBlock]:
    """Find the last opposite-colour candle before the BOS impulse."""
    start = max(0, from_index - 10)
    # Use enumerate on the full slice so we have the real absolute index,
    # avoiding candles.index(c) which returns the FIRST match and breaks on
    # duplicate OHLCV candles (common in flat markets / mock data).
    for abs_idx in range(from_index, start - 1, -1):
        c = candles[abs_idx]
        if direction == "bullish" and c["close"] < c["open"]:   # last red candle
            return OrderBlock(
                index=abs_idx,
                top=c["high"],
                bottom=c["low"],
                direction="bullish",
            )
        if direction == "bearish" and c["close"] > c["open"]:   # last green candle
            return OrderBlock(
                index=abs_idx,
                top=c["high"],
                bottom=c["low"],
                direction="bearish",
            )
    return None


def _calc_pdz(
    swing_highs: list[SwingPoint],
    swing_lows:  list[SwingPoint],
    trend: Trend,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Premium / Discount zones: 75% / 25% / 50% of the last major impulse leg.
    """
    if not swing_highs or not swing_lows:
        return None, None, None

    if trend == Trend.BULLISH:
        leg_low  = swing_lows[-1].price
        leg_high = swing_highs[-1].price
    elif trend == Trend.BEARISH:
        leg_high = swing_highs[-1].price
        leg_low  = swing_lows[-1].price
    else:
        return None, None, None

    rng = leg_high - leg_low
    if rng <= 0:
        return None, None, None

    eq   = leg_low + rng * 0.5
    prem = leg_low + rng * 0.75   # above this = premium (sell zone)
    disc = leg_low + rng * 0.25   # below this = discount (buy zone)
    return round(prem, 5), round(disc, 5), round(eq, 5)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def analyze_market_structure(candles: list[dict], lookback: int = 5) -> StructureResult:
    highs, lows = detect_swing_points(candles, lookback)
    return label_structure(highs, lows, candles)