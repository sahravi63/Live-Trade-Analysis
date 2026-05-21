"""
AMD Phase Detection — v1
========================
Detects the 3-phase Smart Money Cycle:

  1. ACCUMULATION  — range / consolidation after a trend leg
                     (smart money building positions quietly)

  2. MANIPULATION  — fake breakout / stop hunt AGAINST the coming move
                     (shaking out retail before the real leg)

  3. DISTRIBUTION  — the real directional move (expansion after manipulation)
                     This is where we want to be trading WITH smart money.

For SELL setups (your example):
  Major trend = DOWN
  Pullback forms HH → CHoCH
  AMD cycle: range (accumulation of short positions) → fake push up
             (manipulation, hunting buy stops above the range) →
             then price turns and forms LH → LL resuming the downtrend.

  ENTRY: on the return to the LH area (manipulation extreme) where
         an FVG or OB sits — tight SL above manipulation high,
         large TP to the next sell-side liquidity pool.

Supports: Gold (XAUUSD), major FX pairs
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class AMDPhase(str, Enum):
    ACCUMULATION = "ACCUMULATION"
    MANIPULATION = "MANIPULATION"
    DISTRIBUTION = "DISTRIBUTION"
    UNKNOWN      = "UNKNOWN"


@dataclass
class RangeBox:
    high: float
    low: float
    start_index: int
    end_index: int
    bars: int

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def size(self) -> float:
        return self.high - self.low


@dataclass
class ManipulationEvent:
    direction: str          # "bullish_hunt" (swept sells) | "bearish_hunt" (swept buys)
    extreme: float          # the wick high/low of the manipulation
    close: float            # close after manipulation (back inside range)
    candle_index: int
    range_box: RangeBox     # the accumulation range that was violated


@dataclass
class AMDResult:
    phase: AMDPhase
    bias: str                          # "bullish" | "bearish" | "neutral"
    accumulation_range: Optional[RangeBox]
    manipulation: Optional[ManipulationEvent]
    distribution_started: bool

    # Entry context
    entry_zone_high: Optional[float] = None   # for SELL: just above manip low
    entry_zone_low: Optional[float]  = None   # for SELL: manip low / FVG top
    manipulation_sl: Optional[float] = None   # SL above manip high (SELL) / below manip low (BUY)

    # Scoring
    phase_confidence: float = 0.0
    notes: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _calc_atr(candles: list[dict], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent) if recent else 0.001


def _find_consolidation(
    candles: list[dict],
    lookback: int = 30,
    max_range_atr_mult: float = 1.5,
    min_bars: int = 5,
) -> Optional[RangeBox]:
    """
    Find the most recent consolidation range in the last `lookback` bars.
    A consolidation: max_high - min_low <= max_range_atr_mult × ATR
    and the range persists for at least min_bars.
    """
    atr = _calc_atr(candles)
    segment = candles[-lookback:] if len(candles) >= lookback else candles
    start_offset = max(0, len(candles) - lookback)

    best: Optional[tuple] = None  # (start, end, high, low)

    for i in range(len(segment) - min_bars):
        for j in range(i + min_bars, len(segment)):
            chunk = segment[i: j + 1]
            rng_high = max(c["high"] for c in chunk)
            rng_low  = min(c["low"]  for c in chunk)
            rng_size = rng_high - rng_low
            if rng_size <= atr * max_range_atr_mult:
                if best is None or (j - i) > (best[1] - best[0]):
                    best = (i, j, rng_high, rng_low)

    if best is None:
        return None

    i, j, rng_high, rng_low = best
    return RangeBox(
        high=round(rng_high, 5),
        low=round(rng_low, 5),
        start_index=start_offset + i,
        end_index=start_offset + j,
        bars=j - i + 1,
    )


def _find_manipulation(
    candles: list[dict],
    acc_range: RangeBox,
    atr: float,
) -> Optional[ManipulationEvent]:
    """
    After the accumulation range, look for a candle that:
    - Wicks BEYOND the range high (bearish hunt) or range low (bullish hunt)
    - CLOSES back INSIDE the range (the sweep)
    The extreme (wick) becomes the SL reference.
    """
    search = candles[acc_range.end_index:]
    for i, c in enumerate(search):
        abs_idx = acc_range.end_index + i

        # Bearish manipulation: wick above range high, close below
        if (c["high"] > acc_range.high + atr * 0.1
                and c["close"] < acc_range.high):
            return ManipulationEvent(
                direction="bearish_hunt",
                extreme=c["high"],
                close=c["close"],
                candle_index=abs_idx,
                range_box=acc_range,
            )

        # Bullish manipulation: wick below range low, close above
        if (c["low"] < acc_range.low - atr * 0.1
                and c["close"] > acc_range.low):
            return ManipulationEvent(
                direction="bullish_hunt",
                extreme=c["low"],
                close=c["close"],
                candle_index=abs_idx,
                range_box=acc_range,
            )

    return None


def _distribution_started(
    candles: list[dict],
    manip: ManipulationEvent,
    atr: float,
) -> bool:
    """
    Distribution = price has moved decisively AWAY from manipulation extreme
    in the expected direction after the manipulation candle.

    Bearish hunt → expect price to fall → distribution = close < manip.close - 0.5×ATR
    Bullish hunt → expect price to rise → distribution = close > manip.close + 0.5×ATR
    """
    post = candles[manip.candle_index + 1:]
    if not post:
        return False

    for c in post:
        if manip.direction == "bearish_hunt":
            if c["close"] < manip.close - atr * 0.5:
                return True
        else:
            if c["close"] > manip.close + atr * 0.5:
                return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def analyze_amd(
    candles: list[dict],
    current_price: float,
    lookback: int = 40,
) -> AMDResult:
    """
    Classify the current market phase as Accumulation / Manipulation / Distribution
    and return the relevant entry zone, SL reference, and bias.

    Logic:
      1. Find the most recent consolidation range (Accumulation).
      2. If a manipulation (stop hunt) follows → Manipulation phase.
      3. If price has moved past the manipulation → Distribution phase.
      4. Entry is offered only at the DISTRIBUTION phase when price
         retraces to the manipulation zone (FVG/OB inside it).
    """
    if len(candles) < 20:
        return AMDResult(
            phase=AMDPhase.UNKNOWN, bias="neutral",
            accumulation_range=None, manipulation=None,
            distribution_started=False, phase_confidence=0.0,
            notes=["Not enough candles"],
        )

    atr   = _calc_atr(candles)
    notes = []

    # ── 1. Accumulation ───────────────────────────────────────────────────────
    acc = _find_consolidation(candles, lookback=lookback, min_bars=5)
    if acc is None:
        return AMDResult(
            phase=AMDPhase.UNKNOWN, bias="neutral",
            accumulation_range=None, manipulation=None,
            distribution_started=False, phase_confidence=0.1,
            notes=["No consolidation range found — trend may be too clean or fast"],
        )

    notes.append(
        f"Accumulation range: {acc.low:.5f} – {acc.high:.5f} "
        f"({acc.bars} bars, {acc.size/atr:.1f}×ATR)"
    )

    # ── 2. Manipulation ───────────────────────────────────────────────────────
    manip = _find_manipulation(candles, acc, atr)
    if manip is None:
        # Still in accumulation
        price_in_range = acc.low <= current_price <= acc.high
        return AMDResult(
            phase=AMDPhase.ACCUMULATION,
            bias="neutral",
            accumulation_range=acc,
            manipulation=None,
            distribution_started=False,
            phase_confidence=0.5 if price_in_range else 0.3,
            notes=notes + ["Waiting for manipulation (stop hunt) to signal entry direction"],
        )

    notes.append(
        f"Manipulation: {manip.direction} at {manip.extreme:.5f} "
        f"(candle #{manip.candle_index})"
    )

    # Bias is OPPOSITE to manipulation direction
    # Bearish hunt (buys swept above) → SELL bias
    # Bullish hunt (sells swept below) → BUY bias
    bias = "bearish" if manip.direction == "bearish_hunt" else "bullish"

    # ── 3. Distribution ───────────────────────────────────────────────────────
    dist_started = _distribution_started(candles, manip, atr)

    # Entry zone for SELL (bearish hunt):
    #   We want to SHORT when price pulls back UP to the manipulation extreme
    #   Entry zone: between acc.high and manip.extreme (the FVG / OB area)
    #   SL: above manip.extreme + small buffer
    # Entry zone for BUY (bullish hunt):
    #   We want to LONG when price pulls back DOWN to the manipulation extreme
    #   Entry zone: between manip.extreme and acc.low
    #   SL: below manip.extreme - small buffer

    if bias == "bearish":
        entry_zone_low  = round(acc.high, 5)              # retracement to broken range top
        entry_zone_high = round(manip.extreme * 0.9998, 5) # just below manip wick
        manip_sl        = round(manip.extreme + atr * 0.25, 5)
    else:
        entry_zone_high = round(acc.low, 5)
        entry_zone_low  = round(manip.extreme * 1.0002, 5)
        manip_sl        = round(manip.extreme - atr * 0.25, 5)

    phase = AMDPhase.DISTRIBUTION if dist_started else AMDPhase.MANIPULATION
    confidence = 0.85 if dist_started else 0.65

    notes.append(
        f"Bias: {bias.upper()} | Distribution started: {dist_started}"
    )
    if dist_started:
        notes.append(
            f"Wait for pullback into {entry_zone_low:.5f}–{entry_zone_high:.5f} to enter"
        )

    return AMDResult(
        phase=phase,
        bias=bias,
        accumulation_range=acc,
        manipulation=manip,
        distribution_started=dist_started,
        entry_zone_high=entry_zone_high,
        entry_zone_low=entry_zone_low,
        manipulation_sl=manip_sl,
        phase_confidence=round(confidence, 2),
        notes=notes,
    )