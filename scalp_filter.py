"""
Scalping Execution Filter — v1
===============================
Answers ONE question before any order is placed:
"Is market microstructure safe to enter RIGHT NOW?"

Checks (all must pass for SCALP_OK):
  1. Volatility gate     — ATR% above minimum, below blow-off ceiling
  2. Spread filter       — bid/ask spread ≤ max_spread_pips
  3. Session filter      — only trade during liquid sessions (London/NY overlap preferred)
  4. Momentum filter     — last N candles must agree with bias (no chop)
  5. Candle body filter  — avoid doji / indecision bars as entry candles
  6. Volume spike guard  — discard entries right after an abnormal spike

All thresholds are configurable. Defaults tuned for EURUSD M1/M5 scalping.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, time
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScalpFilterResult:
    ok: bool                         # True = safe to enter
    failed_checks: list[str]         # names of failed checks
    passed_checks: list[str]         # names of passed checks
    spread_pips: Optional[float]     # measured spread
    atr_pct: float                   # ATR as % of price
    session: str                     # e.g. "London/NY Overlap"
    momentum_score: float            # 0–1  (1 = full agreement)
    notes: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Session Classifier
# ──────────────────────────────────────────────────────────────────────────────

def classify_session(dt: Optional[datetime] = None) -> tuple[str, float]:
    """
    Returns (session_name, liquidity_score 0–1).
    Uses UTC time.

    Fix: Sydney is only returned when no higher-priority session is active.
    Previously in_sydney fired during 00:00–06:00 UTC which overlaps Tokyo,
    causing the elif chain to hide the bug — now guarded explicitly.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    t = dt.time()

    in_london = time(7, 0) <= t < time(16, 0)
    in_ny     = time(12, 0) <= t < time(21, 0)
    in_tokyo  = time(0, 0) <= t < time(9, 0)
    # Sydney: 21:00–06:00 UTC, but only when Tokyo/London/NY are NOT active
    in_sydney_raw = t >= time(21, 0) or t < time(6, 0)
    in_sydney = in_sydney_raw and not in_tokyo and not in_london and not in_ny

    if in_london and in_ny:
        return "London/NY Overlap", 1.0
    elif in_london:
        return "London", 0.85
    elif in_ny:
        return "New York", 0.80
    elif in_tokyo:
        return "Tokyo", 0.55
    elif in_sydney:
        return "Sydney", 0.40
    else:
        return "Off-Hours", 0.15


# ──────────────────────────────────────────────────────────────────────────────
# Individual Checks
# ──────────────────────────────────────────────────────────────────────────────

def _check_spread(
    spread_pips: Optional[float],
    max_spread_pips: float,
) -> tuple[bool, str]:
    if spread_pips is None:
        # Unknown spread must NOT silently pass — caller must provide spread for
        # live trading.  Treat as a soft block: ok=False so the filter fails,
        # but the label is distinct so the caller can choose to override.
        return False, "spread_unknown (provide spread_pips for live trading)"
    ok = spread_pips <= max_spread_pips
    label = "spread_ok" if ok else f"spread_too_wide ({spread_pips:.1f} > {max_spread_pips})"
    return ok, label


def _check_volatility(
    atr: float,
    current_price: float,
    min_atr_pct: float,   # e.g. 0.0003 = 0.03% of price
    max_atr_pct: float,   # e.g. 0.003  = 0.30% — blow-off guard
) -> tuple[bool, float, str]:
    atr_pct = atr / current_price if current_price > 0 else 0
    ok = min_atr_pct <= atr_pct <= max_atr_pct
    if atr_pct < min_atr_pct:
        label = f"vol_too_low (atr%={atr_pct:.4%} < {min_atr_pct:.4%})"
    elif atr_pct > max_atr_pct:
        label = f"vol_too_high (atr%={atr_pct:.4%} > {max_atr_pct:.4%})"
    else:
        label = "volatility_ok"
    return ok, atr_pct, label


def _check_session(
    session_score: float,
    min_liquidity_score: float,
) -> tuple[bool, str]:
    ok = session_score >= min_liquidity_score
    return ok, ("session_ok" if ok else f"session_illiquid (score={session_score:.2f})")


def _check_momentum(
    candles: list[dict],
    bias: str,                # "bullish" | "bearish"
    lookback: int = 5,
) -> tuple[bool, float, str]:
    """
    Fraction of last N candles that agree with bias (close > open for bull).
    Require at least 60% agreement.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if not recent:
        return True, 0.5, "momentum_unknown"

    if bias == "bullish":
        agree = sum(1 for c in recent if c["close"] >= c["open"])
    else:
        agree = sum(1 for c in recent if c["close"] <= c["open"])

    score = agree / len(recent)
    ok = score >= 0.6
    label = f"momentum_ok ({score:.0%})" if ok else f"momentum_weak ({score:.0%})"
    return ok, score, label


def _check_last_candle_body(
    candles: list[dict],
    min_body_ratio: float = 0.35,   # body must be ≥35% of candle range
) -> tuple[bool, str]:
    """Avoid entering on doji / spinning top candles — no commitment."""
    if not candles:
        return True, "body_unknown"
    c = candles[-1]
    rng = c["high"] - c["low"]
    body = abs(c["close"] - c["open"])
    ratio = body / rng if rng > 0 else 1.0
    ok = ratio >= min_body_ratio
    label = f"body_ok ({ratio:.0%})" if ok else f"doji_candle ({ratio:.0%} body)"
    return ok, label


def _check_volume_spike(
    candles: list[dict],
    lookback: int = 20,
    spike_multiplier: float = 3.0,
) -> tuple[bool, str]:
    """
    If the last candle's volume is >3× average of prior N candles, it's a news spike.
    Skip: entering after a spike usually means chasing.
    """
    if len(candles) < lookback + 1:
        return True, "volume_unknown"
    recent_vols = [c.get("volume", 0) for c in candles[-(lookback + 1):-1]]
    last_vol    = candles[-1].get("volume", 0)
    if not any(v > 0 for v in recent_vols):
        return True, "volume_unknown"        # broker doesn't provide volume
    avg_vol = sum(recent_vols) / len(recent_vols)
    ok = last_vol <= avg_vol * spike_multiplier
    label = "volume_ok" if ok else f"volume_spike ({last_vol:.0f} vs avg {avg_vol:.0f})"
    return ok, label


# ──────────────────────────────────────────────────────────────────────────────
# Main Filter
# ──────────────────────────────────────────────────────────────────────────────

def run_scalp_filter(
    candles: list[dict],
    bias: str,                          # "bullish" | "bearish"
    atr: float,
    current_price: float,
    spread_pips: Optional[float] = None,
    dt: Optional[datetime] = None,
    # Thresholds
    max_spread_pips: float = 1.5,       # EURUSD ~0.8 pips typical; 1.5 is generous
    min_atr_pct: float = 0.00025,       # ~2.5 pips on EURUSD — not in dead flat market
    max_atr_pct: float = 0.003,         # ~30 pips — avoid news blow-off
    min_liquidity_score: float = 0.55,  # Tokyo or better
    momentum_lookback: int = 5,
    require_session: bool = True,
    require_momentum: bool = True,
    require_body: bool = True,
    require_volume: bool = True,
) -> ScalpFilterResult:
    """
    Run all scalping execution filters.
    Returns ScalpFilterResult with ok=True only if ALL enabled checks pass.
    """
    failed  = []
    passed  = []
    notes   = []

    # 1. Spread
    spread_ok, spread_label = _check_spread(spread_pips, max_spread_pips)
    if "spread_unknown" in spread_label:
        notes.append("spread_pips not provided — spread filter blocking. Pass spread_pips= for live trading.")
        failed.append(spread_label)
    elif spread_ok:
        passed.append(spread_label)
    else:
        failed.append(spread_label)

    # 2. Volatility
    vol_ok, atr_pct, vol_label = _check_volatility(atr, current_price, min_atr_pct, max_atr_pct)
    (passed if vol_ok else failed).append(vol_label)

    # 3. Session
    session_name, session_score = classify_session(dt)
    if require_session:
        sess_ok, sess_label = _check_session(session_score, min_liquidity_score)
        (passed if sess_ok else failed).append(sess_label)
        if not sess_ok:
            notes.append(f"Current session: {session_name} — wait for London/NY")
    else:
        passed.append("session_check_disabled")

    # 4. Momentum
    if require_momentum:
        mom_ok, mom_score, mom_label = _check_momentum(candles, bias, momentum_lookback)
        (passed if mom_ok else failed).append(mom_label)
    else:
        mom_score = 0.5
        passed.append("momentum_check_disabled")

    # 5. Last candle body
    if require_body:
        body_ok, body_label = _check_last_candle_body(candles)
        (passed if body_ok else failed).append(body_label)
    else:
        passed.append("body_check_disabled")

    # 6. Volume spike
    if require_volume:
        vol_spike_ok, vol_spike_label = _check_volume_spike(candles)
        (passed if vol_spike_ok else failed).append(vol_spike_label)
    else:
        passed.append("volume_check_disabled")

    return ScalpFilterResult(
        ok=len(failed) == 0,
        failed_checks=failed,
        passed_checks=passed,
        spread_pips=spread_pips,
        atr_pct=atr_pct,
        session=session_name,
        momentum_score=mom_score if require_momentum else 0.5,
        notes=notes,
    )