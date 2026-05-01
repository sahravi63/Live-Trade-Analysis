"""
Liquidity Detection
Detects: Equal Highs/Lows (liquidity pools), Stop Hunts (wicks through key levels)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class LiquidityLevel:
    price: float
    level_type: str        # "equal_high" | "equal_low"
    touches: int
    swept: bool = False
    sweep_candle_index: Optional[int] = None


@dataclass
class StopHunt:
    candle_index: int
    direction: str         # "bullish_sweep" (hunted sell stops below) | "bearish_sweep" (hunted buy stops above)
    swept_level: float
    close: float


@dataclass
class LiquidityResult:
    levels: list[LiquidityLevel]
    stop_hunts: list[StopHunt]
    nearest_buy_side: Optional[float]    # liquidity above (equal highs)
    nearest_sell_side: Optional[float]   # liquidity below (equal lows)


def detect_equal_levels(
    candles: list[dict],
    tolerance_pips: float = 0.0003,
    min_touches: int = 2,
) -> list[LiquidityLevel]:
    """
    Find price levels where highs or lows cluster (liquidity pools).
    tolerance_pips: max distance between prices to be considered "equal"
    """
    levels: list[LiquidityLevel] = []

    # --- Equal Highs ---
    highs = [c["high"] for c in candles]
    used = [False] * len(highs)
    for i in range(len(highs)):
        if used[i]:
            continue
        cluster = [highs[i]]
        for j in range(i + 1, len(highs)):
            if abs(highs[j] - highs[i]) <= tolerance_pips:
                cluster.append(highs[j])
                used[j] = True
        if len(cluster) >= min_touches:
            levels.append(LiquidityLevel(
                price=sum(cluster) / len(cluster),
                level_type="equal_high",
                touches=len(cluster),
            ))

    # --- Equal Lows ---
    lows = [c["low"] for c in candles]
    used = [False] * len(lows)
    for i in range(len(lows)):
        if used[i]:
            continue
        cluster = [lows[i]]
        for j in range(i + 1, len(lows)):
            if abs(lows[j] - lows[i]) <= tolerance_pips:
                cluster.append(lows[j])
                used[j] = True
        if len(cluster) >= min_touches:
            levels.append(LiquidityLevel(
                price=sum(cluster) / len(cluster),
                level_type="equal_low",
                touches=len(cluster),
            ))

    return levels


def detect_stop_hunts(
    candles: list[dict],
    levels: list[LiquidityLevel],
    tolerance_pips: float = 0.0003,
) -> tuple[list[StopHunt], list[LiquidityLevel]]:
    """
    A stop hunt occurs when price wicks through a liquidity level but closes back inside.
    Returns hunts and updates swept status on levels.
    """
    hunts: list[StopHunt] = []

    for i, candle in enumerate(candles):
        for level in levels:
            if level.swept:
                continue

            # Bearish sweep: wick above equal high, closes below it
            if (
                level.level_type == "equal_high"
                and candle["high"] > level.price + tolerance_pips
                and candle["close"] < level.price
            ):
                hunts.append(StopHunt(
                    candle_index=i,
                    direction="bearish_sweep",
                    swept_level=level.price,
                    close=candle["close"],
                ))
                level.swept = True
                level.sweep_candle_index = i

            # Bullish sweep: wick below equal low, closes above it
            elif (
                level.level_type == "equal_low"
                and candle["low"] < level.price - tolerance_pips
                and candle["close"] > level.price
            ):
                hunts.append(StopHunt(
                    candle_index=i,
                    direction="bullish_sweep",
                    swept_level=level.price,
                    close=candle["close"],
                ))
                level.swept = True
                level.sweep_candle_index = i

    return hunts, levels


def analyze_liquidity(
    candles: list[dict],
    current_price: float,
    tolerance_pips: float = 0.0003,
) -> LiquidityResult:
    levels = detect_equal_levels(candles, tolerance_pips)
    hunts, levels = detect_stop_hunts(candles, levels, tolerance_pips)

    # Find nearest unswept buy-side (above) and sell-side (below) liquidity
    unswept = [l for l in levels if not l.swept]

    buy_side = [l.price for l in unswept if l.level_type == "equal_high" and l.price > current_price]
    sell_side = [l.price for l in unswept if l.level_type == "equal_low" and l.price < current_price]

    return LiquidityResult(
        levels=levels,
        stop_hunts=hunts,
        nearest_buy_side=min(buy_side) if buy_side else None,
        nearest_sell_side=max(sell_side) if sell_side else None,
    )
