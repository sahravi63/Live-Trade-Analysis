"""
FastAPI Backend — v2
Serves SMC analysis results to the React dashboard.
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import traceback

from data_connector import fetch_candles, MT5_AVAILABLE
from market_structure import analyze_market_structure
from liquidity import analyze_liquidity
from displacement import analyze_displacement, analyze_retracement
from decision_engine import decide

app = FastAPI(title="SMC Trading Engine", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Response Models ────────────────────────────────────────────────────────────

class FVGModel(BaseModel):
    top: float
    bottom: float
    direction: str
    filled: bool
    mitigated: bool
    strength: float

class LiquidityLevelModel(BaseModel):
    price: float
    level_type: str
    touches: int
    swept: bool

class StopHuntModel(BaseModel):
    candle_index: int
    direction: str
    swept_level: float

class SwingPointModel(BaseModel):
    index: int
    price: float
    is_high: bool
    label: Optional[str] = None

class OrderBlockModel(BaseModel):
    index: int
    top: float
    bottom: float
    direction: str
    broken: bool

class AnalysisResponse(BaseModel):
    symbol: str
    timeframe: str
    current_price: float
    # Structure
    trend: str
    last_event: Optional[str] = None
    bos_level: Optional[float] = None
    choch_level: Optional[float] = None
    swing_highs: list[SwingPointModel]
    swing_lows: list[SwingPointModel]
    order_blocks: list[OrderBlockModel]
    # Premium / Discount / EQ zones
    premium_zone: Optional[float] = None
    discount_zone: Optional[float] = None
    equilibrium: Optional[float] = None
    # Liquidity
    liquidity_levels: list[LiquidityLevelModel]
    stop_hunts: list[StopHuntModel]
    nearest_buy_side: Optional[float] = None
    nearest_sell_side: Optional[float] = None
    # Displacement / FVG
    fvgs: list[FVGModel]
    displacement_direction: Optional[str] = None
    displacement_strength: float
    ote_low: Optional[float] = None
    ote_high: Optional[float] = None
    # Retracement
    is_retracing: bool
    retracement_pct: float
    inside_fvg: bool
    inside_ote: bool
    retracement_strength: str
    # Signal
    signal: str
    confidence: float
    entry_zone: list[float]
    stop_loss: float
    take_profit: float
    rr_ratio: float
    confluence_score: int
    reasons: list[str]
    warnings: list[str]
    # Candles
    candles: list[dict]
    mt5_connected: bool


@app.get("/analyze", response_model=AnalysisResponse)
def analyze(
    symbol: str = Query("EURUSD"),
    timeframe: str = Query("H1"),
    count: int = Query(200, ge=50, le=500),
    use_mock: bool = Query(False),
):
    try:
        candles = fetch_candles(symbol=symbol, timeframe=timeframe, count=count, use_mock=use_mock)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data fetch error: {str(e)}")

    try:
        current_price = candles[-1]["close"]

        structure    = analyze_market_structure(candles)
        liquidity    = analyze_liquidity(candles, current_price)
        displacement = analyze_displacement(candles)
        retracement  = analyze_retracement(candles, displacement.fvgs, current_price, displacement)
        signal       = decide(candles, structure, liquidity, displacement, retracement, current_price)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")

    return AnalysisResponse(
        symbol=symbol,
        timeframe=timeframe,
        current_price=current_price,
        # Structure
        trend=structure.trend.value,
        last_event=structure.last_event.value if structure.last_event else None,
        bos_level=structure.bos_level,
        choch_level=structure.choch_level,
        swing_highs=[
            SwingPointModel(index=s.index, price=s.price, is_high=True,
                            label=s.label.value if s.label else None)
            for s in structure.swing_highs[-10:]
        ],
        swing_lows=[
            SwingPointModel(index=s.index, price=s.price, is_high=False,
                            label=s.label.value if s.label else None)
            for s in structure.swing_lows[-10:]
        ],
        order_blocks=[
            OrderBlockModel(index=ob.index, top=ob.top, bottom=ob.bottom,
                            direction=ob.direction, broken=ob.broken)
            for ob in structure.order_blocks
        ],
        premium_zone=structure.premium_zone,
        discount_zone=structure.discount_zone,
        equilibrium=structure.equilibrium,
        # Liquidity
        liquidity_levels=[
            LiquidityLevelModel(price=l.price, level_type=l.level_type,
                                touches=l.touches, swept=l.swept)
            for l in liquidity.levels[:20]
        ],
        stop_hunts=[
            StopHuntModel(candle_index=h.candle_index, direction=h.direction,
                          swept_level=h.swept_level)
            for h in liquidity.stop_hunts[-5:]
        ],
        nearest_buy_side=liquidity.nearest_buy_side,
        nearest_sell_side=liquidity.nearest_sell_side,
        # Displacement
        fvgs=[
            FVGModel(top=f.top, bottom=f.bottom, direction=f.direction,
                     filled=f.filled, mitigated=f.mitigated, strength=f.strength)
            for f in displacement.fvgs[-6:]
        ],
        displacement_direction=displacement.last_displacement_dir,
        displacement_strength=displacement.displacement_strength,
        ote_low=displacement.ote_low,
        ote_high=displacement.ote_high,
        # Retracement
        is_retracing=retracement.is_retracing,
        retracement_pct=retracement.retracement_pct,
        inside_fvg=retracement.inside_fvg,
        inside_ote=retracement.inside_ote,
        retracement_strength=retracement.retracement_strength,
        # Signal
        signal=signal.signal.value,
        confidence=signal.confidence,
        entry_zone=list(signal.entry_zone),
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        rr_ratio=signal.rr_ratio,
        confluence_score=signal.confluence_score,
        reasons=signal.reasons,
        warnings=signal.warnings,
        candles=candles[-200:],
        mt5_connected=MT5_AVAILABLE,
    )


@app.get("/health")
def health():
    return {"status": "ok", "mt5_available": MT5_AVAILABLE}


@app.get("/symbols")
def symbols():
    return {
        "pairs": [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "AUDUSD", "NZDUSD", "USDCAD", "GBPJPY",
            "EURJPY", "EURGBP", "XAUUSD",
        ]
    }