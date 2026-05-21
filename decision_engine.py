"""
Decision Engine — v3
====================
Enhancements over v2:
  ✦ AMD Phase integration (Accumulation → Manipulation → Distribution)
  ✦ LH (Lower High) SELL entry on pullback during downtrend
  ✦ HL (Higher Low) BUY entry on pullback during uptrend
  ✦ Gold (XAUUSD) pip / lot handling
  ✦ Manipulation extreme used as precision SL (tighter than swing)
  ✦ Suppression zone: no BUY above LH, no SELL below HL
  ✦ Cleaner entry context: AMD zone → FVG → OTE priority

Scenario handled (per your spec):
  Major trend DOWN → pullback with HH → CHoCH confirms → LLs form
  → pullback to LH area → FVG / OB / manipulation zone inside
  → SELL entry, SL above LH wick (manipulation extreme)
  → TP to next sell-side liquidity pool

Example (Gold):
  Major trend: Bearish
  Pullback high (LH / manipulation extreme): 4566
  Entry zone (FVG inside LH area): 4563
  TP (sell-side pool / next LL): 4540
  R:R = (4563-4540)/(4566-4563) = 23/3 ≈ 7.7
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from market_structure import StructureResult, StructureEvent, Trend
from liquidity import LiquidityResult
from displacement import DisplacementResult, RetracementResult
from amd_phases import AMDResult, AMDPhase


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
    amd_phase: str = "UNKNOWN"
    setup_type: str = ""          # e.g. "LH_SELL", "HL_BUY", "CHOCH_REVERSAL"


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


def _is_gold(symbol: str) -> bool:
    return "XAU" in symbol.upper() or "GOLD" in symbol.upper()


def _pip_size(symbol: str) -> float:
    if _is_gold(symbol):
        return 0.01       # Gold: 1 pip = $0.01
    if "JPY" in symbol.upper():
        return 0.01
    return 0.0001


def _recent_swing_low(structure: StructureResult, n: int = 3) -> Optional[float]:
    lows = structure.swing_lows[-n:] if structure.swing_lows else []
    return min((s.price for s in lows), default=None)


def _recent_swing_high(structure: StructureResult, n: int = 3) -> Optional[float]:
    highs = structure.swing_highs[-n:] if structure.swing_highs else []
    return max((s.price for s in highs), default=None)


def _last_lower_high(structure: StructureResult) -> Optional[float]:
    """Return the most recent LH swing point (for SELL setup)."""
    for sh in reversed(structure.swing_highs):
        if sh.label and sh.label.value == "LH":
            return sh.price
    # Fallback: last swing high
    if structure.swing_highs:
        return structure.swing_highs[-1].price
    return None


def _last_higher_low(structure: StructureResult) -> Optional[float]:
    """Return the most recent HL swing point (for BUY setup)."""
    for sl in reversed(structure.swing_lows):
        if sl.label and sl.label.value == "HL":
            return sl.price
    if structure.swing_lows:
        return structure.swing_lows[-1].price
    return None


def _sl_for_sell_lh(
    structure: StructureResult,
    amd: Optional[AMDResult],
    current_price: float,
    atr: float,
    symbol: str = "EURUSD",
) -> float:
    """
    SL for a SELL at LH:
    Priority: AMD manipulation extreme > swing high + buffer
    """
    pip = _pip_size(symbol)

    # AMD gives us the exact manipulation wick — use that
    if amd and amd.manipulation and amd.manipulation.direction == "bearish_hunt":
        return round(amd.manipulation.extreme + atr * 0.2, 5)

    # Fall back to the last swing high
    lh = _last_lower_high(structure)
    if lh and lh > current_price:
        return round(lh + atr * 0.25, 5)

    return round(current_price + atr * 1.5, 5)


def _sl_for_buy_hl(
    structure: StructureResult,
    amd: Optional[AMDResult],
    current_price: float,
    atr: float,
    symbol: str = "EURUSD",
) -> float:
    """
    SL for a BUY at HL:
    Priority: AMD manipulation extreme > swing low - buffer
    """
    if amd and amd.manipulation and amd.manipulation.direction == "bullish_hunt":
        return round(amd.manipulation.extreme - atr * 0.2, 5)

    hl = _last_higher_low(structure)
    if hl and hl < current_price:
        return round(hl - atr * 0.25, 5)

    return round(current_price - atr * 1.5, 5)


def _sl_for_buy(structure, current_price, atr, amd=None, symbol="EURUSD"):
    return _sl_for_buy_hl(structure, amd, current_price, atr, symbol)


def _sl_for_sell(structure, current_price, atr, amd=None, symbol="EURUSD"):
    return _sl_for_sell_lh(structure, amd, current_price, atr, symbol)


def _tp_for_buy(current_price, sl, liquidity: LiquidityResult, min_rr: float) -> float:
    risk = current_price - sl
    minimum_tp = round(current_price + risk * min_rr, 5)
    if liquidity.nearest_buy_side and liquidity.nearest_buy_side > current_price:
        candidate = round(liquidity.nearest_buy_side - 0.00005, 5)
        if (candidate - current_price) >= risk * min_rr:
            return candidate
    return minimum_tp


def _tp_for_sell(current_price, sl, liquidity: LiquidityResult, min_rr: float) -> float:
    risk = sl - current_price
    minimum_tp = round(current_price - risk * min_rr, 5)
    if liquidity.nearest_sell_side and liquidity.nearest_sell_side < current_price:
        candidate = round(liquidity.nearest_sell_side + 0.00005, 5)
        if (current_price - candidate) >= risk * min_rr:
            return candidate
    return minimum_tp


def _price_near_lh(current_price: float, lh_price: Optional[float], atr: float) -> bool:
    """Is price pulling back into the LH zone (within 1.5×ATR)?"""
    if lh_price is None:
        return False
    return abs(current_price - lh_price) <= atr * 1.5


def _price_near_hl(current_price: float, hl_price: Optional[float], atr: float) -> bool:
    if hl_price is None:
        return False
    return abs(current_price - hl_price) <= atr * 1.5


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
    amd: Optional[AMDResult] = None,
    min_rr: float = 1.8,
    min_confidence: float = 0.45,
    symbol: str = "EURUSD",
) -> TradeSignal:
    """
    Scoring system (max = 100+):

    Hard-gate conditions (any failure → WAIT):
      ✗ No clear structure
      ✗ Displacement direction ≠ trend direction (unless CHoCH reversal)
      ✗ No FVG or OB near price
      ✗ BUY in premium zone / SELL in discount zone
      ✗ BUY above LH / SELL below HL (trap zones)

    Setup types detected:
      LH_SELL      — pullback to Lower High in downtrend → short with AMD SL
      HL_BUY       — pullback to Higher Low in uptrend → long with AMD SL
      CHOCH_REVERSAL — CHoCH forming, entering the new direction early
      BOS_CONTINUATION — clean BOS with FVG retracement

    AMD Integration:
      +20  AMD Distribution phase (manipulation confirmed, price expanding)
      +15  AMD Manipulation phase (sweep confirmed, entry setup forming)
      +10  Price inside AMD entry zone
      +10  Manipulation SL is tighter than swing SL (better R:R)
    """
    score    = 0
    reasons  = []
    warnings = []
    bias     = Signal.WAIT
    setup_type = ""
    atr      = calculate_atr(candles)
    amd_phase_str = amd.phase.value if amd else "UNKNOWN"

    # ── STEP 1: Determine directional bias ────────────────────────────────────
    dir_bull = (
        structure.trend == Trend.BULLISH
        and displacement.last_displacement_dir == "bullish"
    )
    dir_bear = (
        structure.trend == Trend.BEARISH
        and displacement.last_displacement_dir == "bearish"
    )
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
        setup_type = "CHOCH_REVERSAL" if choch_bull else "BOS_CONTINUATION"
    elif dir_bear or choch_bear:
        bias = Signal.SELL
        setup_type = "CHOCH_REVERSAL" if choch_bear else "BOS_CONTINUATION"
    else:
        warnings.append(
            f"Trend ({structure.trend.value}) ≠ displacement "
            f"({displacement.last_displacement_dir or 'none'}) — no edge"
        )
        return _wait_signal(current_price, atr, reasons, warnings, amd_phase_str)

    # ── STEP 2: AMD Phase check ───────────────────────────────────────────────
    if amd:
        if amd.phase == AMDPhase.DISTRIBUTION:
            # Best: we're in distribution, price is moving in expected direction
            # Look for pullback entry
            amd_bias_matches = (
                (amd.bias == "bearish" and bias == Signal.SELL) or
                (amd.bias == "bullish" and bias == Signal.BUY)
            )
            if amd_bias_matches:
                score += 20
                reasons.append(
                    f"AMD Distribution phase confirmed ({amd.bias.upper()} bias)"
                )
                # Refine setup type
                if bias == Signal.SELL:
                    setup_type = "LH_SELL"
                else:
                    setup_type = "HL_BUY"
            else:
                warnings.append(
                    f"AMD bias ({amd.bias}) conflicts with signal ({bias.value})"
                )
                score -= 10

        elif amd.phase == AMDPhase.MANIPULATION:
            amd_bias_matches = (
                (amd.bias == "bearish" and bias == Signal.SELL) or
                (amd.bias == "bullish" and bias == Signal.BUY)
            )
            if amd_bias_matches:
                score += 15
                reasons.append(
                    f"AMD Manipulation phase — stop hunt confirmed, "
                    f"distribution imminent ({amd.bias.upper()})"
                )
                setup_type = "LH_SELL" if bias == Signal.SELL else "HL_BUY"
            else:
                score -= 5

        elif amd.phase == AMDPhase.ACCUMULATION:
            # Still in range — caution
            warnings.append("AMD: still in Accumulation range — no manipulation yet")
            score -= 5

        # Price inside AMD entry zone = extra confluence
        if (amd.entry_zone_low and amd.entry_zone_high
                and amd.entry_zone_low <= current_price <= amd.entry_zone_high):
            score += 10
            reasons.append(
                f"Price inside AMD entry zone "
                f"({amd.entry_zone_low:.5f} – {amd.entry_zone_high:.5f})"
            )

    # ── STEP 3: LH / HL Proximity check ──────────────────────────────────────
    lh_price = _last_lower_high(structure)
    hl_price = _last_higher_low(structure)

    if bias == Signal.SELL:
        if lh_price and _price_near_lh(current_price, lh_price, atr):
            score += 15
            reasons.append(
                f"Price at/near Lower High ({lh_price:.5f}) — "
                "ideal SELL zone in downtrend pullback"
            )
            setup_type = "LH_SELL"
        elif lh_price and current_price > lh_price:
            warnings.append(
                f"Price {current_price:.5f} above LH {lh_price:.5f} — "
                "potential trap, BUY bias at this level"
            )
            score -= 15

    if bias == Signal.BUY:
        if hl_price and _price_near_hl(current_price, hl_price, atr):
            score += 15
            reasons.append(
                f"Price at/near Higher Low ({hl_price:.5f}) — "
                "ideal BUY zone in uptrend pullback"
            )
            setup_type = "HL_BUY"
        elif hl_price and current_price < hl_price:
            warnings.append(
                f"Price {current_price:.5f} below HL {hl_price:.5f} — "
                "potential trap, SELL bias at this level"
            )
            score -= 15

    # ── STEP 4: Premium / Discount zone filter ────────────────────────────────
    eq = structure.equilibrium
    if eq:
        if bias == Signal.BUY and current_price < eq:
            score += 10
            reasons.append("Price in discount zone (below EQ) — favorable BUY")
        elif bias == Signal.BUY and structure.premium_zone and current_price >= structure.premium_zone:
            warnings.append("BUY attempted in premium zone — lower probability")
            score -= 10
        elif bias == Signal.SELL and current_price > eq:
            score += 10
            reasons.append("Price in premium zone (above EQ) — favorable SELL")
        elif bias == Signal.SELL and structure.discount_zone and current_price <= structure.discount_zone:
            warnings.append("SELL attempted in discount zone — lower probability")
            score -= 10

    # ── STEP 5: Structure event ───────────────────────────────────────────────
    if structure.last_event == StructureEvent.BOS:
        score += 25
        reasons.append(f"BOS confirmed at {structure.bos_level}")
    elif structure.last_event == StructureEvent.CHOCH:
        score += 20
        reasons.append(f"CHoCH detected at {structure.choch_level} — reversal setup")

    # ── STEP 6: FVG confluence ────────────────────────────────────────────────
    active_fvgs = [f for f in displacement.fvgs if not f.filled]
    if not active_fvgs:
        warnings.append("No active FVG — entry confluence missing")
        return _wait_signal(current_price, atr, reasons, warnings, amd_phase_str)

    if retracement.inside_fvg and retracement.fvg_zone:
        fvg = retracement.fvg_zone
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

    # ── STEP 7: OTE zone ──────────────────────────────────────────────────────
    if retracement.inside_ote:
        score += 10
        reasons.append(
            f"Price inside OTE zone "
            f"({displacement.ote_low:.5f} – {displacement.ote_high:.5f})"
        )

    # ── STEP 8: Liquidity sweep ───────────────────────────────────────────────
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

    # ── STEP 9: Retracement quality ───────────────────────────────────────────
    if retracement.retracement_strength == "weak":
        score += 10
        reasons.append(f"Clean pullback ({retracement.retracement_pct:.1f}%) — momentum intact")
    elif retracement.retracement_strength == "strong":
        score -= 10
        warnings.append(f"Deep retracement ({retracement.retracement_pct:.1f}%) — FVG may fully fill")

    # ── STEP 10: Displacement strength ────────────────────────────────────────
    if displacement.displacement_strength >= 0.65:
        score += 10
        reasons.append(f"Strong impulse displacement ({displacement.displacement_strength:.0%})")
    elif displacement.displacement_strength >= 0.40:
        score += 5

    # ── STEP 11: Order Block confluence ───────────────────────────────────────
    for ob in structure.order_blocks:
        if ob.broken:
            continue
        if bias == Signal.BUY and ob.direction == "bullish" and ob.bottom <= current_price <= ob.top:
            score += 8
            reasons.append(f"Price inside bullish OB ({ob.bottom:.5f} – {ob.top:.5f})")
            break
        if bias == Signal.SELL and ob.direction == "bearish" and ob.bottom <= current_price <= ob.top:
            score += 8
            reasons.append(f"Price inside bearish OB ({ob.bottom:.5f} – {ob.top:.5f})")
            break

    # Multi-confluence bonus
    if len(reasons) >= 4:
        score += 5

    confidence = round(min(max(score / 100, 0.0), 1.0), 3)

    # ── Build trade levels ────────────────────────────────────────────────────
    # Priority: AMD zone > FVG > OTE > ATR fallback
    if (amd and amd.entry_zone_low and amd.entry_zone_high
            and amd.phase in (AMDPhase.MANIPULATION, AMDPhase.DISTRIBUTION)):
        entry_low  = amd.entry_zone_low
        entry_high = amd.entry_zone_high
    else:
        entry_low  = retracement.entry_low  or round(current_price - atr * 0.2, 5)
        entry_high = retracement.entry_high or round(current_price + atr * 0.2, 5)

    entry_mid = (entry_low + entry_high) / 2

    if bias == Signal.BUY:
        sl = _sl_for_buy(structure, entry_low, atr, amd, symbol)
        tp = _tp_for_buy(entry_mid, sl, liquidity, min_rr)
    else:
        sl = _sl_for_sell(structure, entry_high, atr, amd, symbol)
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
        amd_phase=amd_phase_str,
        setup_type=setup_type,
    )


def _wait_signal(
    current_price: float,
    atr: float,
    reasons: list[str],
    warnings: list[str],
    amd_phase: str = "UNKNOWN",
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
        amd_phase=amd_phase,
        setup_type="",
    )