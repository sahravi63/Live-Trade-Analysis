"""
FastAPI Backend — v4
Serves SMC + AMD analysis + scalping execution layer to the React dashboard.
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
from amd_phases import analyze_amd
from decision_engine import decide
from scalp_orchestrator import ScalpOrchestrator

app = FastAPI(title="SMC Trading Engine", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_orchestrator = ScalpOrchestrator(account_balance=10_000)


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

class ScalpFilterModel(BaseModel):
    ok: bool
    failed_checks: list[str]
    passed_checks: list[str]
    spread_pips: Optional[float]
    atr_pct: float
    session: str
    momentum_score: float
    notes: list[str]

class SizingModel(BaseModel):
    allowed: bool
    lots: float
    units: int
    risk_amount: float
    risk_pct: float
    sl_pips: float
    sl_adjusted: float
    reason: str

class AccumulationRangeModel(BaseModel):
    high: float
    low: float
    mid: float
    start_index: int
    end_index: int
    bars: int

class ManipulationModel(BaseModel):
    direction: str
    extreme: float
    close: float
    candle_index: int

class AMDModel(BaseModel):
    phase: str
    bias: str
    distribution_started: bool
    phase_confidence: float
    entry_zone_low: Optional[float]
    entry_zone_high: Optional[float]
    manipulation_sl: Optional[float]
    accumulation_range: Optional[AccumulationRangeModel]
    manipulation: Optional[ManipulationModel]
    notes: list[str]

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
    setup_type: str
    # AMD Phase
    amd: AMDModel
    # Scalping layer
    scalp_filter: ScalpFilterModel
    sizing: SizingModel
    ready_to_trade: bool
    block_reasons: list[str]
    exec_entry: Optional[float]
    exec_sl: Optional[float]
    exec_tp: Optional[float]
    scalp_summary: str
    # Raw data
    candles: list[dict]
    mt5_connected: bool


@app.get("/analyze", response_model=AnalysisResponse)
def analyze(
    symbol: str = Query("EURUSD"),
    timeframe: str = Query("H1"),
    count: int = Query(200, ge=50, le=500),
    use_mock: bool = Query(False),
    spread_pips: Optional[float] = Query(None),
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
        amd          = analyze_amd(candles, current_price)

        signal = decide(
            candles, structure, liquidity, displacement, retracement,
            current_price, amd=amd, symbol=symbol,
        )

        scalp = _orchestrator.evaluate(
            candles=candles,
            structure=structure,
            liquidity=liquidity,
            displacement=displacement,
            retracement=retracement,
            current_price=current_price,
            symbol=symbol,
            timeframe=timeframe,
            spread_pips=spread_pips,
        )

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")

    # Build AMD model
    acc = amd.accumulation_range
    manip = amd.manipulation
    amd_model = AMDModel(
        phase=amd.phase.value,
        bias=amd.bias,
        distribution_started=amd.distribution_started,
        phase_confidence=amd.phase_confidence,
        entry_zone_low=amd.entry_zone_low,
        entry_zone_high=amd.entry_zone_high,
        manipulation_sl=amd.manipulation_sl,
        accumulation_range=AccumulationRangeModel(
            high=acc.high, low=acc.low, mid=acc.mid,
            start_index=acc.start_index, end_index=acc.end_index, bars=acc.bars,
        ) if acc else None,
        manipulation=ManipulationModel(
            direction=manip.direction,
            extreme=manip.extreme,
            close=manip.close,
            candle_index=manip.candle_index,
        ) if manip else None,
        notes=amd.notes,
    )

    return AnalysisResponse(
        symbol=symbol,
        timeframe=timeframe,
        current_price=current_price,
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
        fvgs=[
            FVGModel(top=f.top, bottom=f.bottom, direction=f.direction,
                     filled=f.filled, mitigated=f.mitigated, strength=f.strength)
            for f in displacement.fvgs[-6:]
        ],
        displacement_direction=displacement.last_displacement_dir,
        displacement_strength=displacement.displacement_strength,
        ote_low=displacement.ote_low,
        ote_high=displacement.ote_high,
        is_retracing=retracement.is_retracing,
        retracement_pct=retracement.retracement_pct,
        inside_fvg=retracement.inside_fvg,
        inside_ote=retracement.inside_ote,
        retracement_strength=retracement.retracement_strength,
        signal=signal.signal.value,
        confidence=signal.confidence,
        entry_zone=list(signal.entry_zone),
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        rr_ratio=signal.rr_ratio,
        confluence_score=signal.confluence_score,
        reasons=signal.reasons,
        warnings=signal.warnings,
        setup_type=signal.setup_type,
        amd=amd_model,
        scalp_filter=ScalpFilterModel(
            ok=scalp.scalp_filter.ok,
            failed_checks=scalp.scalp_filter.failed_checks,
            passed_checks=scalp.scalp_filter.passed_checks,
            spread_pips=scalp.scalp_filter.spread_pips,
            atr_pct=scalp.scalp_filter.atr_pct,
            session=scalp.scalp_filter.session,
            momentum_score=scalp.scalp_filter.momentum_score,
            notes=scalp.scalp_filter.notes,
        ),
        sizing=SizingModel(
            allowed=scalp.sizing.allowed,
            lots=scalp.sizing.lots,
            units=scalp.sizing.units,
            risk_amount=scalp.sizing.risk_amount,
            risk_pct=scalp.sizing.risk_pct,
            sl_pips=scalp.sizing.sl_pips,
            sl_adjusted=scalp.sizing.sl_adjusted,
            reason=scalp.sizing.reason,
        ),
        ready_to_trade=scalp.ready_to_trade,
        block_reasons=scalp.block_reasons,
        exec_entry=scalp.exec_entry,
        exec_sl=scalp.exec_sl,
        exec_tp=scalp.exec_tp,
        scalp_summary=scalp.summary,
        candles=candles[-200:],
        mt5_connected=MT5_AVAILABLE,
    )


@app.post("/trade/open")
def trade_open(trade_id: str, entry: float):
    _orchestrator.confirm_entry(trade_id, entry)
    return {"status": "recorded", "trade_id": trade_id}


@app.post("/trade/close")
def trade_close(trade_id: str, pnl: float):
    _orchestrator.close_trade(trade_id, pnl)
    return {"status": "recorded", "trade_id": trade_id, "pnl": pnl}


@app.get("/risk/status")
def risk_status():
    return _orchestrator.risk_status()


@app.post("/risk/reset")
def risk_reset(new_balance: Optional[float] = None):
    _orchestrator.reset_day(new_balance)
    return {"status": "reset", "balance": _orchestrator.risk_mgr.balance}


@app.get("/health")
def health():
    return {"status": "ok", "mt5_available": MT5_AVAILABLE, "version": "4.0.0"}


@app.get("/symbols")
def symbols():
    return {
        "pairs": [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "AUDUSD", "NZDUSD", "USDCAD", "GBPJPY",
            "EURJPY", "EURGBP", "XAUUSD",
        ]
    }