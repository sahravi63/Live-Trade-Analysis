"""
FastAPI Backend — v5
Serves SMC + AMD analysis + scalping execution layer to the React dashboard.

Changes over v4:
  ✦ oscillator_data forwarded to AnalysisResponse for chart panel
  ✦ AMD v2 fields (mss_detected, mss_level, divergence_detected,
    divergence_indices) added to AMDModel and response
  ✦ /analyze query param `momentum_period` (default 14) exposed
"""

import json
import os
import threading

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal, Optional
import traceback

from data_connector import fetch_candles, MT5_AVAILABLE
from market_structure import analyze_market_structure
from liquidity import analyze_liquidity
from displacement import analyze_displacement, analyze_retracement
from amd_phases import analyze_amd
from decision_engine import decide
from scalp_orchestrator import ScalpOrchestrator
from bot import MarketBot, load_config
from timemachine import historical_replay
import data_connector as dc

app = FastAPI(title="SMC Trading Engine + Market Bot", version="6.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_orchestrator = ScalpOrchestrator(account_balance=10_000)
# WARNING: _orchestrator is a module-level singleton holding mutable session state
# (daily P&L, open trades, cooldown timestamps).  This is only safe with a single
# Uvicorn worker process.  If you add --workers N or run under gunicorn with N workers,
# each worker gets its own independent state and /trade/open|close calls will be
# silently routed to different instances.
# Fix for multi-worker: move RiskState into Redis or a shared DB before scaling.


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

class OscillatorPoint(BaseModel):
    time: str
    value: float

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
    # v2 additions
    mss_detected: bool = False
    mss_level: Optional[float] = None
    divergence_detected: bool = False
    divergence_indices: list[int] = []
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
    # Oscillator (for chart)
    oscillator_data: list[OscillatorPoint]
    # Raw data
    candles: list[dict]
    mt5_connected: bool


class ManualPaperTradeRequest(BaseModel):
    symbol: str
    timeframe: str
    direction: Literal["BUY", "SELL"]
    signal: Literal["BUY", "SELL", "WAIT"]
    entry: float
    stop_loss: float
    take_profit: float
    bar_time: str
    setup: str = "MANUAL"
    confidence: float = 0.0


class PaperFundsRequest(BaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)


class PaperPositionUpdateRequest(BaseModel):
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    lots: float = Field(gt=0, allow_inf_nan=False)
    bar_time: str


@app.get("/analyze", response_model=AnalysisResponse)
def analyze(
    symbol: str = Query("EURUSD"),
    timeframe: str = Query("H1"),
    count: int = Query(200, ge=50, le=500),
    use_mock: bool = Query(False),
    spread_pips: Optional[float] = Query(None),
    momentum_period: int = Query(14, ge=5, le=50),
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
        amd          = analyze_amd(candles, current_price, momentum_period=momentum_period)

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
            amd=amd,            # reuse already-computed AMD — avoids double call
        )

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Analysis error: {str(e)}")

    # Build AMD model
    acc   = amd.accumulation_range
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
        mss_detected=amd.mss_detected,
        mss_level=amd.mss_level,
        divergence_detected=amd.divergence_detected,
        divergence_indices=amd.divergence_indices,
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
        oscillator_data=[
            OscillatorPoint(time=str(d["time"]), value=d["value"])
            for d in amd.oscillator_data[-200:]
        ],
        candles=candles[-200:],
        mt5_connected=dc.last_source() == "mt5",
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
    return {"status": "ok", "mt5_available": MT5_AVAILABLE,
            "mt5_connected": dc.mt5_connected(), "data_source": dc.last_source(),
            "version": "6.0.0"}


# ══════════════════════════════════════════════════════════════════════════════
# Bot control — the automated scanner/paper-trader from bot.py
# ══════════════════════════════════════════════════════════════════════════════

_bot: Optional[MarketBot] = None
_paper_monitor_stop = threading.Event()
_paper_monitor_thread: Optional[threading.Thread] = None


def get_bot() -> MarketBot:
    global _bot
    if _bot is None:
        _bot = MarketBot(load_config())
    return _bot


def _paper_monitor_loop() -> None:
    while not _paper_monitor_stop.wait(15):
        try:
            bot = get_bot()
            if not bot.running and not bot.external_running():
                bot.monitor_open_positions()
        except Exception as e:
            print(f"[paper-monitor] could not monitor open positions: {e}")


@app.on_event("startup")
def start_paper_monitor() -> None:
    global _paper_monitor_thread
    if _paper_monitor_thread and _paper_monitor_thread.is_alive():
        return
    _paper_monitor_stop.clear()
    _paper_monitor_thread = threading.Thread(
        target=_paper_monitor_loop,
        name="paper-position-monitor",
        daemon=True,
    )
    _paper_monitor_thread.start()


@app.on_event("shutdown")
def stop_paper_monitor() -> None:
    _paper_monitor_stop.set()
    if _paper_monitor_thread:
        _paper_monitor_thread.join(timeout=2)


@app.get("/bot/status")
def bot_status():
    return get_bot().status()


@app.post("/bot/start")
def bot_start():
    b = get_bot()
    if b.external_running():
        raise HTTPException(409, "The bot is already running in a terminal (python bot.py run).")
    try:
        started = b.start()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": started, "running": True}


@app.post("/bot/stop")
def bot_stop():
    b = get_bot()
    if not b.running and b.external_running():
        raise HTTPException(409, "The bot is running in a terminal — stop it there with Ctrl+C.")
    return {"stopped": b.stop()}


@app.post("/bot/scan-once")
def bot_scan_once():
    b = get_bot()
    if b.running or b.external_running():
        raise HTTPException(409, "Bot loop is running — it scans on its own.")
    return {"rows": b.scan_once(), "scanned_at": b.last_scan_at}


@app.post("/bot/manual-trade")
def bot_manual_trade(request: ManualPaperTradeRequest):
    b = get_bot()
    if b.external_running():
        raise HTTPException(
            409, "A bot is running in a terminal; stop it before placing a manual paper trade.",
        )
    try:
        position = b.open_manual_paper_trade(
            symbol=request.symbol,
            timeframe=request.timeframe,
            direction=request.direction,
            entry=request.entry,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            bar_time=request.bar_time,
            setup=request.setup,
            confidence=request.confidence,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e

    return {"status": "opened", "position": position.__dict__}


@app.post("/bot/positions/{position_id}/close")
def bot_close_position(position_id: str):
    b = get_bot()
    if b.external_running():
        raise HTTPException(409, "A bot is running in a terminal; close positions through that bot's API process.")
    try:
        position = b.close_paper_position(position_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "closed", "position": position.__dict__}


@app.patch("/bot/positions/{position_id}")
def bot_update_position(position_id: str, request: PaperPositionUpdateRequest):
    b = get_bot()
    if b.external_running():
        raise HTTPException(409, "A bot is running in a terminal; adjust positions through that bot's API process.")
    try:
        position = b.update_paper_position(
            position_id,
            request.stop_loss,
            request.take_profit,
            request.lots,
            request.bar_time,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "updated", "position": position.__dict__}


@app.get("/bot/journal")
def bot_journal(limit: int = Query(50, ge=1, le=500)):
    path = get_bot().journal_path
    if not os.path.exists(path):
        return {"events": []}
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    return {"events": [json.loads(l) for l in reversed(lines) if l.strip()]}


@app.post("/bot/reset-paper")
def bot_reset_paper():
    b = get_bot()
    if b.running or b.external_running():
        raise HTTPException(409, "Stop the bot before resetting the paper account.")
    bal = b.cfg["account"]["balance"]
    b.broker.reset(bal)
    b.orch.reset_day(bal)
    b._save_risk()
    return {"status": "reset", "balance": bal}


def _adjust_paper_funds(action: str, request: PaperFundsRequest):
    b = get_bot()
    if b.external_running():
        raise HTTPException(
            409, "A bot is running in a terminal; adjust paper funds through that bot's API process.",
        )
    try:
        return {"status": action, **b.adjust_paper_funds(action, request.amount)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/bot/paper-account/deposit")
def paper_account_deposit(request: PaperFundsRequest):
    return _adjust_paper_funds("deposit", request)


@app.post("/bot/paper-account/withdraw")
def paper_account_withdraw(request: PaperFundsRequest):
    return _adjust_paper_funds("withdraw", request)


@app.get("/bot/backtest")
def bot_backtest(symbol: str = "EURUSD", timeframe: str = "M5",
                 count: int = Query(1000, ge=300, le=5000), use_mock: bool = False):
    cfg = load_config()
    candles = fetch_candles(symbol, timeframe, count, use_mock=use_mock)
    rep = historical_replay(candles, symbol, None, horizon=cfg["timemachine"]["horizon_bars"],
                            min_rr=cfg["filters"]["min_rr"],
                            min_confidence=cfg["filters"]["min_confidence"],
                            max_samples=max(100, len(candles) - 200))
    return {"symbol": symbol, "timeframe": timeframe, "bars": len(candles),
            "source": dc.last_source(), **rep.to_dict()}


@app.get("/candles")
def candles(
    symbol: str = Query("EURUSD"),
    timeframe: str = Query("D1"),
    count: int = Query(5, ge=2, le=500),
    use_mock: bool = Query(False),
):
    """Raw OHLC only — used by the dashboard for higher-timeframe high/low levels."""
    try:
        c = fetch_candles(symbol=symbol, timeframe=timeframe, count=count, use_mock=use_mock)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data fetch error: {str(e)}")
    return {"symbol": symbol, "timeframe": timeframe, "source": dc.last_source(), "candles": c}


@app.get("/symbols")
def symbols():
    return {
        "pairs": [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "AUDUSD", "NZDUSD", "USDCAD", "GBPJPY",
            "EURJPY", "EURGBP", "XAUUSD",
        ]
    }