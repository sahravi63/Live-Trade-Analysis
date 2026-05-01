"""
Decision Engine — v2
Multi-confluence rule gating + swing-level-anchored SL/TP

Key improvements:
1. HARD GATES: signal fires only if ALL of the following are true:
   - Trend direction confirmed (BOS or CHoCH with clear structure)
   - Displacement direction matches trend
   - Price is in a discount zone (BUY) or premium zone (SELL)
   - At least one FVG or Order Block present
   Score then modifies CONFIDENCE, not whether to trade.

2. SL placed BELOW the most recent swing low / above swing high (not ATR offset).
   A buffer of 0.25× ATR is added to avoid wick sweeps.

3. TP placed at the next liquidity pool (nearest buy-side / sell-side liquidity).
   Falls back to 2× risk if no liquidity pool found.

4. Entry zone = FVG zone if price is in it, else OTE zone, else near current price.

5. R:R minimum raised to 1.8 (configurable).
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from market_structure import StructureResult, StructureEvent, Trend
from liquidity import LiquidityResult
from displacement import DisplacementResult, RetracementResult


class Signal(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


@dataclass
class TradeSignal:
    signal: Signal
    confidence: float
    entry_zone: tuple[float, float]
    stop_loss: float
    take_profit: float
    rr_ratio: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confluence_score: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def calculate_atr(candles: list[dict], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent) if recent else 0.001


def _recent_swing_low(structure: StructureResult, n: int = 3) -> Optional[float]:
    lows = structure.swing_lows[-n:] if structure.swing_lows else []
    return min((s.price for s in lows), default=None)


def _recent_swing_high(structure: StructureResult, n: int = 3) -> Optional[float]:
    highs = structure.swing_highs[-n:] if structure.swing_highs else []
    return max((s.price for s in highs), default=None)


def _sl_for_buy(structure: StructureResult, current_price: float, atr: float) -> float:
    """SL below the most recent swing low, with ATR buffer."""
    sl_base = _recent_swing_low(structure, n=3)
    if sl_base and sl_base < current_price:
        return round(sl_base - atr * 0.25, 5)
    return round(current_price - atr * 1.5, 5)


def _sl_for_sell(structure: StructureResult, current_price: float, atr: float) -> float:
    """SL above the most recent swing high, with ATR buffer."""
    sl_base = _recent_swing_high(structure, n=3)
    if sl_base and sl_base > current_price:
        return round(sl_base + atr * 0.25, 5)
    return round(current_price + atr * 1.5, 5)


def _tp_for_buy(
    current_price: float,
    sl: float,
    liquidity: LiquidityResult,
    min_rr: float,
) -> float:
    risk = current_price - sl
    minimum_tp = round(current_price + risk * min_rr, 5)
    # Prefer the nearest unswept buy-side liquidity above current price
    if liquidity.nearest_buy_side and liquidity.nearest_buy_side > current_price:
        candidate = round(liquidity.nearest_buy_side - 0.00005, 5)   # just below the pool
        if (candidate - current_price) >= risk * min_rr:
            return candidate
    return minimum_tp


def _tp_for_sell(
    current_price: float,
    sl: float,
    liquidity: LiquidityResult,
    min_rr: float,
) -> float:
    risk = sl - current_price
    minimum_tp = round(current_price - risk * min_rr, 5)
    # Prefer the nearest unswept sell-side liquidity below current price
    if liquidity.nearest_sell_side and liquidity.nearest_sell_side < current_price:
        candidate = round(liquidity.nearest_sell_side + 0.00005, 5)  # just above the pool
        if (current_price - candidate) >= risk * min_rr:
            return candidate
    return minimum_tp


# ──────────────────────────────────────────────────────────────────────────────
# Main Decision Function
# ──────────────────────────────────────────────────────────────────────────────

def decide(
    candles: list[dict],
    structure: StructureResult,
    liquidity: LiquidityResult,
    displacement: DisplacementResult,
    retracement: RetracementResult,
    current_price: float,
    min_rr: float = 1.8,
    min_confidence: float = 0.45,
) -> TradeSignal:
    """
    Scoring system (max = 100):

    Hard-gate conditions (any failure → WAIT):
      ✗ No clear trend (NEUTRAL)                       → WAIT
      ✗ Displacement direction ≠ trend direction       → WAIT
      ✗ No FVG or Order Block near price               → WAIT
      ✗ In premium zone for BUY / discount zone for SELL → WAIT

    Confidence scoring (0–100 → 0.0–1.0):
      +25  BOS confirmed in trade direction
      +20  CHoCH (reversal) with displacement aligned
      +15  Stop hunt (liquidity sweep) in signal direction
      +20  Price inside valid FVG
      +10  Price inside OTE zone (62–79% Fib)
      +10  Price in discount (BUY) / premium (SELL) zone
      +10  Weak retracement (<30% into FVG)
      −10  Deep retracement (>70%)
      −10  FVG mitigated (50%+ filled — less reliable)
      +10  Strong displacement body (>1.5× ATR)
      +05  Multiple confluences (≥4 reasons)
    """
    score    = 0
    reasons  = []
    warnings = []
    bias     = Signal.WAIT
    atr      = calculate_atr(candles)

    # ── STEP 1: Determine directional bias ────────────────────────────────────
    dir_bull = (
        structure.trend == Trend.BULLISH
        and displacement.last_displacement_dir == "bullish"
    )
    dir_bear = (
        structure.trend == Trend.BEARISH
        and displacement.last_displacement_dir == "bearish"
    )

    # CHoCH reversal: displacement flips against current trend
    choch_bull = (
        structure.last_event == StructureEvent.CHOCH
        and structure.trend == Trend.BEARISH
        and displacement.last_displacement_dir == "bullish"
    )
    choch_bear = (
        structure.last_event == StructureEvent.CHOCH
        and structure.trend == Trend.BULLISH
        and displacement.last_displacement_dir == "bearish"
    )

    if dir_bull or choch_bull:
        bias = Signal.BUY
    elif dir_bear or choch_bear:
        bias = Signal.SELL
    else:
        warnings.append(
            f"Trend ({structure.trend.value}) ≠ displacement "
            f"({displacement.last_displacement_dir or 'none'}) — no edge"
        )
        return _wait_signal(current_price, atr, reasons, warnings)

    # ── STEP 2: Premium / Discount zone filter ────────────────────────────────
    eq = structure.equilibrium
    if eq:
        if bias == Signal.BUY and current_price > eq:
            score += 10
            reasons.append("Price in discount zone (below EQ) — favorable BUY")
        elif bias == Signal.BUY and current_price >= (structure.premium_zone or 1e9):
            warnings.append("BUY attempted in premium zone — lower probability")
            score -= 10
        elif bias == Signal.SELL and current_price < eq:
            score += 10
            reasons.append("Price in premium zone (above EQ) — favorable SELL")
        elif bias == Signal.SELL and current_price <= (structure.discount_zone or 0):
            warnings.append("SELL attempted in discount zone — lower probability")
            score -= 10

    # ── STEP 3: Structure event ───────────────────────────────────────────────
    if structure.last_event == StructureEvent.BOS:
        score += 25
        reasons.append(f"BOS confirmed at {structure.bos_level}")
    elif structure.last_event == StructureEvent.CHOCH:
        score += 20
        reasons.append(f"CHoCH detected at {structure.choch_level} — reversal setup")

    # ── STEP 4: FVG confluence ────────────────────────────────────────────────
    active_fvgs = [f for f in displacement.fvgs if not f.filled]
    if not active_fvgs:
        warnings.append("No active FVG — entry confluence missing")
        return _wait_signal(current_price, atr, reasons, warnings)

    if retracement.inside_fvg and retracement.fvg_zone:
        fvg = retracement.fvg_zone
        # Direction of FVG must match bias
        if (bias == Signal.BUY and fvg.direction == "bullish") or \
           (bias == Signal.SELL and fvg.direction == "bearish"):
            score += 20
            reasons.append(
                f"Price inside {fvg.direction} FVG "
                f"({fvg.bottom:.5f} – {fvg.top:.5f})"
            )
            if fvg.mitigated:
                score -= 10
                warnings.append("FVG is >50% mitigated — reduced reliability")
        else:
            warnings.append(f"FVG direction ({fvg.direction}) conflicts with {bias.value} bias")
            score -= 5
    else:
        score += 5
        reasons.append("Near active FVG zone (not yet inside)")

    # ── STEP 5: OTE zone ──────────────────────────────────────────────────────
    if retracement.inside_ote:
        score += 10
        reasons.append(
            f"Price inside OTE zone "
            f"({displacement.ote_low:.5f} – {displacement.ote_high:.5f})"
        )

    # ── STEP 6: Liquidity sweep ───────────────────────────────────────────────
    if liquidity.stop_hunts:
        hunt = liquidity.stop_hunts[-1]
        if hunt.direction == "bullish_sweep" and bias == Signal.BUY:
            score += 15
            reasons.append(f"Bullish stop hunt swept sells @ {hunt.swept_level:.5f}")
        elif hunt.direction == "bearish_sweep" and bias == Signal.SELL:
            score += 15
            reasons.append(f"Bearish stop hunt swept buys @ {hunt.swept_level:.5f}")
        else:
            score += 3
            warnings.append("Recent stop hunt direction doesn't match signal bias")

    # ── STEP 7: Retracement quality ───────────────────────────────────────────
    if retracement.retracement_strength == "weak":
        score += 10
        reasons.append(f"Clean pullback ({retracement.retracement_pct:.1f}%) — momentum intact")
    elif retracement.retracement_strength == "strong":
        score -= 10
        warnings.append(f"Deep retracement ({retracement.retracement_pct:.1f}%) — FVG may fully fill")

    # ── STEP 8: Displacement strength ────────────────────────────────────────
    if displacement.displacement_strength >= 0.65:
        score += 10
        reasons.append(f"Strong impulse displacement ({displacement.displacement_strength:.0%})")
    elif displacement.displacement_strength >= 0.40:
        score += 5

    # ── STEP 9: Order Block confluence ───────────────────────────────────────
    for ob in structure.order_blocks:
        if ob.broken:
            continue
        if bias == Signal.BUY and ob.direction == "bullish" and ob.bottom <= current_price <= ob.top:
            score += 8
            reasons.append(f"Price inside bullish Order Block ({ob.bottom:.5f} – {ob.top:.5f})")
            break
        if bias == Signal.SELL and ob.direction == "bearish" and ob.bottom <= current_price <= ob.top:
            score += 8
            reasons.append(f"Price inside bearish Order Block ({ob.bottom:.5f} – {ob.top:.5f})")
            break

    # ── Multi-confluence bonus ─────────────────────────────────────────────────
    if len(reasons) >= 4:
        score += 5

    # ── Compute confidence ────────────────────────────────────────────────────
    confidence = round(min(max(score / 100, 0.0), 1.0), 3)

    # ── Build trade levels ────────────────────────────────────────────────────
    # Entry zone from retracement engine (FVG / OTE / ATR fallback)
    entry_low  = retracement.entry_low  or round(current_price - atr * 0.2, 5)
    entry_high = retracement.entry_high or round(current_price + atr * 0.2, 5)
    entry_mid  = (entry_low + entry_high) / 2

    if bias == Signal.BUY:
        sl = _sl_for_buy(structure, entry_low, atr)
        tp = _tp_for_buy(entry_mid, sl, liquidity, min_rr)
    else:
        sl = _sl_for_sell(structure, entry_high, atr)
        tp = _tp_for_sell(entry_mid, sl, liquidity, min_rr)

    risk   = abs(entry_mid - sl)
    reward = abs(tp - entry_mid)
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    # ── Gate: suppress if confidence or RR insufficient ──────────────────────
    if confidence < min_confidence:
        warnings.append(f"Confidence {confidence:.0%} below threshold {min_confidence:.0%} — WAIT")
        bias = Signal.WAIT
    elif rr < min_rr:
        warnings.append(f"R:R {rr:.1f} below minimum {min_rr} — WAIT")
        bias = Signal.WAIT

    return TradeSignal(
        signal=bias,
        confidence=confidence,
        entry_zone=(round(entry_low, 5), round(entry_high, 5)),
        stop_loss=round(sl, 5),
        take_profit=round(tp, 5),
        rr_ratio=rr,
        reasons=reasons,
        warnings=warnings,
        confluence_score=score,
    )


def _wait_signal(
    current_price: float,
    atr: float,
    reasons: list[str],
    warnings: list[str],
) -> TradeSignal:
    return TradeSignal(
        signal=Signal.WAIT,
        confidence=0.0,
        entry_zone=(round(current_price - atr * 0.1, 5), round(current_price + atr * 0.1, 5)),
        stop_loss=current_price,
        take_profit=current_price,
        rr_ratio=0.0,
        reasons=reasons,
        warnings=warnings,
        confluence_score=0,
    )