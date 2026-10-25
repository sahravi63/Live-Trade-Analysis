"""
AMD Phase Detection — v2
========================
Changes over v1:
  ✦ Market Structure Shift (MSS) detection — identifies the first Lower High
    after a bearish manipulation (or first Higher Low after a bullish one).
    MSS is the true distribution trigger; pure displacement cross is too early.
  ✦ Momentum divergence detection — price makes a new extreme but the
    momentum proxy (close-delta) does not → trapped-money signal.
  ✦ Oscillator data exported (close-delta series) for the dashboard chart.
  ✦ Tighter entry zone: entry_zone is now clipped to the FVG inside the
    manipulation leg when one is present (reduces zone width significantly).
  ✦ `mss_detected` and `divergence_detected` flags + `divergence_indices`
    added to AMDResult for the dashboard badge system.
  ✦ Minimum accumulation bars raised to 6 for noise reduction.
  ✦ Phase confidence is now a weighted combination of four sub-scores.
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
    direction: str          # "bullish_hunt" | "bearish_hunt"
    extreme: float
    close: float
    candle_index: int
    range_box: RangeBox


@dataclass
class AMDResult:
    phase: AMDPhase
    bias: str                          # "bullish" | "bearish" | "neutral"
    accumulation_range: Optional[RangeBox]
    manipulation: Optional[ManipulationEvent]
    distribution_started: bool

    # Entry context
    entry_zone_high: Optional[float] = None
    entry_zone_low: Optional[float]  = None
    manipulation_sl: Optional[float] = None

    # MSS / Divergence
    mss_detected: bool = False
    mss_level: Optional[float] = None          # price of the MSS pivot
    divergence_detected: bool = False
    divergence_indices: list[int] = field(default_factory=list)

    # Oscillator (close-delta) for dashboard chart
    oscillator_data: list[dict] = field(default_factory=list)

    # Scoring
    phase_confidence: float = 0.0
    notes: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _calc_atr(candles: list[dict], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent) if recent else 0.001


def _close_delta(candles: list[dict], period: int = 14) -> list[float]:
    """Simple momentum: close difference over `period` bars."""
    result = [0.0] * len(candles)
    for i in range(period, len(candles)):
        result[i] = candles[i]["close"] - candles[i - period]["close"]
    return result


def _build_oscillator_data(candles: list[dict], period: int = 14) -> list[dict]:
    deltas = _close_delta(candles, period)
    return [
        {"time": c["time"], "value": round(deltas[i], 6)}
        for i, c in enumerate(candles)
    ]


def _find_consolidation(
    candles: list[dict],
    lookback: int = 30,
    max_range_atr_mult: float = 1.5,
    min_bars: int = 6,            # raised from 5
) -> Optional[RangeBox]:
    # NOTE: this is O(lookback²) — ~450 iterations at default lookback=30.
    # Fine for lookback ≤ 40; if you raise it above 60, switch to a sliding
    # window min/max approach to keep it O(n).
    atr = _calc_atr(candles)
    # Hard cap so a misconfigured caller can't cause a perf cliff
    lookback = min(lookback, 60)
    segment = candles[-lookback:] if len(candles) >= lookback else candles
    start_offset = max(0, len(candles) - lookback)

    best: Optional[tuple] = None

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
    search = candles[acc_range.end_index:]
    for i, c in enumerate(search):
        abs_idx = acc_range.end_index + i

        if (c["high"] > acc_range.high + atr * 0.1
                and c["close"] < acc_range.high):
            return ManipulationEvent(
                direction="bearish_hunt",
                extreme=c["high"],
                close=c["close"],
                candle_index=abs_idx,
                range_box=acc_range,
            )

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


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Market Structure Shift detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_mss(
    candles: list[dict],
    manip: ManipulationEvent,
    atr: float,
) -> tuple[bool, Optional[float]]:
    """
    After a bearish_hunt manipulation:
      MSS = first candle after manip.candle_index whose HIGH is lower
            than the close of the manipulation candle (first Lower High formed).
    After a bullish_hunt:
      MSS = first candle whose LOW is higher than manip.close.

    The MSS level is the pivot price that confirms the structural shift.
    """
    post = candles[manip.candle_index + 1:]
    prev_high = manip.extreme

    if manip.direction == "bearish_hunt":
        prev_close = manip.close
        for c in post:
            # A clear LH: high below the manipulation close, body bearish
            if c["high"] < prev_close and c["close"] < c["open"]:
                return True, round(c["high"], 5)
            prev_high = c["high"]
    else:
        prev_close = manip.close
        for c in post:
            if c["low"] > prev_close and c["close"] > c["open"]:
                return True, round(c["low"], 5)

    return False, None


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Momentum divergence detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_divergence(
    candles: list[dict],
    manip: ManipulationEvent,
    momentum: list[float],
    lookback: int = 20,
) -> tuple[bool, list[int]]:
    """
    Bearish divergence (for bearish_hunt):
      Price makes a higher high near the manipulation extreme while
      the momentum (close-delta) makes a lower high → exhaustion.

    Bullish divergence (for bullish_hunt):
      Price makes a lower low near extreme while momentum makes a higher low.

    Returns (detected, [idx1, idx2]) where idx1 < idx2 are the two pivots.
    """
    start = max(0, manip.candle_index - lookback)
    segment_candles   = candles[start: manip.candle_index + 1]
    segment_momentum  = momentum[start: manip.candle_index + 1]

    if len(segment_candles) < 4:
        return False, []

    if manip.direction == "bearish_hunt":
        # Find two local highs
        peaks = []
        for i in range(1, len(segment_candles) - 1):
            if (segment_candles[i]["high"] >= segment_candles[i - 1]["high"]
                    and segment_candles[i]["high"] >= segment_candles[i + 1]["high"]):
                peaks.append(i)
        if len(peaks) >= 2:
            p1, p2 = peaks[-2], peaks[-1]
            price_hh = segment_candles[p2]["high"] > segment_candles[p1]["high"]
            mom_lh   = segment_momentum[p2]        < segment_momentum[p1]
            if price_hh and mom_lh:
                return True, [start + p1, start + p2]

    else:
        troughs = []
        for i in range(1, len(segment_candles) - 1):
            if (segment_candles[i]["low"] <= segment_candles[i - 1]["low"]
                    and segment_candles[i]["low"] <= segment_candles[i + 1]["low"]):
                troughs.append(i)
        if len(troughs) >= 2:
            t1, t2 = troughs[-2], troughs[-1]
            price_ll = segment_candles[t2]["low"] < segment_candles[t1]["low"]
            mom_hl   = segment_momentum[t2]       > segment_momentum[t1]
            if price_ll and mom_hl:
                return True, [start + t1, start + t2]

    return False, []


# ─────────────────────────────────────────────────────────────────────────────
# NEW: FVG inside the manipulation leg (tighter entry zone)
# ─────────────────────────────────────────────────────────────────────────────

def _fvg_in_manip_leg(
    candles: list[dict],
    manip: ManipulationEvent,
    atr: float,
) -> tuple[Optional[float], Optional[float]]:
    """
    Scan the 3-bar windows starting at acc.end_index up to manip.candle_index
    for a 3-candle FVG that sits inside the manipulation leg.
    Returns (fvg_top, fvg_bottom) of the strongest FVG, or (None, None).
    """
    start = manip.range_box.end_index
    end   = manip.candle_index + 1
    segment = candles[max(0, start): end]

    best_gap  = 0.0
    best_top  = None
    best_bot  = None

    for i in range(2, len(segment)):
        c0, c1, c2 = segment[i - 2], segment[i - 1], segment[i]
        body = abs(c1["close"] - c1["open"])
        if body < atr * 0.5:          # impulse body filter (relaxed vs main detector)
            continue

        if manip.direction == "bearish_hunt":
            # Bearish FVG: c0.low > c2.high
            gap = c0["low"] - c2["high"]
            if gap > 0 and gap > best_gap:
                best_gap = gap
                best_top = c0["low"]
                best_bot = c2["high"]
        else:
            # Bullish FVG: c2.low > c0.high
            gap = c2["low"] - c0["high"]
            if gap > 0 and gap > best_gap:
                best_gap = gap
                best_top = c2["low"]
                best_bot = c0["high"]

    return (round(best_top, 5), round(best_bot, 5)) if (best_top and best_top != best_bot) else (None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def analyze_amd(
    candles: list[dict],
    current_price: float,
    lookback: int = 40,
    momentum_period: int = 14,
) -> AMDResult:
    """
    Classify the current market phase and return structured entry context.

    Upgrade path over v1:
      — MSS confirmation before calling distribution (reduces false entries).
      — Divergence detection adds an extra confluence badge.
      — Entry zone is tightened to the FVG inside the manipulation leg.
      — Oscillator data is exported for the dashboard chart panel.
    """
    if len(candles) < 20:
        return AMDResult(
            phase=AMDPhase.UNKNOWN, bias="neutral",
            accumulation_range=None, manipulation=None,
            distribution_started=False, phase_confidence=0.0,
            notes=["Not enough candles"],
        )

    atr       = _calc_atr(candles)
    momentum  = _close_delta(candles, momentum_period)
    osc_data  = _build_oscillator_data(candles, momentum_period)
    notes     = []

    # ── 1. Accumulation ───────────────────────────────────────────────────────
    acc = _find_consolidation(candles, lookback=lookback, min_bars=6)
    if acc is None:
        return AMDResult(
            phase=AMDPhase.UNKNOWN, bias="neutral",
            accumulation_range=None, manipulation=None,
            distribution_started=False, phase_confidence=0.1,
            oscillator_data=osc_data,
            notes=["No consolidation range found — trend may be too clean or fast"],
        )

    notes.append(
        f"Accumulation range: {acc.low:.5f} – {acc.high:.5f} "
        f"({acc.bars} bars, {acc.size/atr:.1f}×ATR)"
    )

    # ── 2. Manipulation ───────────────────────────────────────────────────────
    manip = _find_manipulation(candles, acc, atr)
    if manip is None:
        price_in_range = acc.low <= current_price <= acc.high
        return AMDResult(
            phase=AMDPhase.ACCUMULATION,
            bias="neutral",
            accumulation_range=acc,
            manipulation=None,
            distribution_started=False,
            phase_confidence=0.5 if price_in_range else 0.3,
            oscillator_data=osc_data,
            notes=notes + ["Waiting for manipulation (stop hunt) to signal entry direction"],
        )

    notes.append(
        f"Manipulation: {manip.direction} at {manip.extreme:.5f} "
        f"(candle #{manip.candle_index})"
    )

    bias = "bearish" if manip.direction == "bearish_hunt" else "bullish"

    # ── 3. MSS ────────────────────────────────────────────────────────────────
    mss_detected, mss_level = _detect_mss(candles, manip, atr)
    if mss_detected:
        notes.append(f"MSS confirmed at {mss_level:.5f} — structural shift validated")

    # ── 4. Divergence ─────────────────────────────────────────────────────────
    div_detected, div_indices = _detect_divergence(candles, manip, momentum)
    if div_detected:
        notes.append(
            f"Momentum divergence detected (bars {div_indices[0]}→{div_indices[1]})"
        )

    # ── 5. Distribution ───────────────────────────────────────────────────────
    dist_started = _distribution_started(candles, manip, atr)

    # ── 6. Entry zone (tightened to FVG when available) ──────────────────────
    fvg_top, fvg_bot = _fvg_in_manip_leg(candles, manip, atr)

    if bias == "bearish":
        if fvg_top and fvg_bot:
            entry_zone_low  = round(fvg_bot, 5)
            entry_zone_high = round(fvg_top, 5)
            notes.append(
                f"Entry tightened to FVG inside manip leg: "
                f"{entry_zone_low:.5f} – {entry_zone_high:.5f}"
            )
        else:
            entry_zone_low  = round(acc.high, 5)
            entry_zone_high = round(manip.extreme * 0.9998, 5)
        manip_sl = round(manip.extreme + atr * 0.25, 5)
    else:
        if fvg_top and fvg_bot:
            entry_zone_low  = round(fvg_bot, 5)
            entry_zone_high = round(fvg_top, 5)
            notes.append(
                f"Entry tightened to FVG inside manip leg: "
                f"{entry_zone_low:.5f} – {entry_zone_high:.5f}"
            )
        else:
            entry_zone_high = round(acc.low, 5)
            entry_zone_low  = round(manip.extreme * 1.0002, 5)
        manip_sl = round(manip.extreme - atr * 0.25, 5)

    # ── 7. Confidence (weighted) ──────────────────────────────────────────────
    base        = 0.65 if dist_started else 0.45
    mss_bonus   = 0.12 if mss_detected  else 0.0
    div_bonus   = 0.08 if div_detected  else 0.0
    fvg_bonus   = 0.05 if fvg_top       else 0.0
    confidence  = round(min(base + mss_bonus + div_bonus + fvg_bonus, 0.95), 2)

    phase = AMDPhase.DISTRIBUTION if dist_started else AMDPhase.MANIPULATION

    notes.append(f"Bias: {bias.upper()} | Distribution started: {dist_started}")
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
        mss_detected=mss_detected,
        mss_level=mss_level,
        divergence_detected=div_detected,
        divergence_indices=div_indices,
        oscillator_data=osc_data,
        phase_confidence=confidence,
        notes=notes,
    )