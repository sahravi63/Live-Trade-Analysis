"""
Scalping Orchestrator — v2
===========================
Adds AMD phase analysis to the pipeline:

  SMC Signal → AMD Phases → Execution Filter → Risk Manager

AMD changes the entry logic:
  - In DISTRIBUTION phase with LH_SELL setup:
      entry = retracement to manipulation zone (FVG inside LH)
      SL    = above manipulation wick (tight — minimal risk)
      TP    = next sell-side liquidity pool (large reward)

  - In ACCUMULATION phase: hold off, label as "waiting for setup"
  - In MANIPULATION phase: alert is set, entry zone pre-computed
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from decision_engine import decide, TradeSignal, Signal, calculate_atr
from market_structure import StructureResult
from liquidity import LiquidityResult
from displacement import DisplacementResult, RetracementResult
from amd_phases import analyze_amd, AMDResult, AMDPhase
from scalp_filter import run_scalp_filter, ScalpFilterResult
from risk_manager import RiskManager, SizingResult


@dataclass
class ScalpDecision:
    symbol: str
    timeframe: str
    evaluated_at: str

    # All layers
    signal: TradeSignal
    amd: AMDResult
    scalp_filter: ScalpFilterResult
    sizing: SizingResult

    # Final verdict
    ready_to_trade: bool
    block_reasons: list[str] = field(default_factory=list)
    summary: str = ""

    # Execution levels (spread-corrected)
    exec_entry: Optional[float] = None
    exec_sl: Optional[float] = None
    exec_tp: Optional[float] = None


class ScalpOrchestrator:

    def __init__(
        self,
        account_balance: float = 10_000.0,
        risk_pct: float = 0.005,
        max_spread_pips: float = 1.5,
        min_atr_pct: float = 0.00025,
        max_atr_pct: float = 0.003,
        min_liquidity_score: float = 0.55,
        min_rr: float = 1.8,
        min_confidence: float = 0.45,
    ):
        self.risk_mgr            = RiskManager(
            account_balance=account_balance,
            risk_pct_per_trade=risk_pct,
        )
        self.max_spread_pips     = max_spread_pips
        self.min_atr_pct         = min_atr_pct
        self.max_atr_pct         = max_atr_pct
        self.min_liquidity_score = min_liquidity_score
        self.min_rr              = min_rr
        self.min_confidence      = min_confidence

    def evaluate(
        self,
        candles: list[dict],
        structure: StructureResult,
        liquidity: LiquidityResult,
        displacement: DisplacementResult,
        retracement: RetracementResult,
        current_price: float,
        symbol: str = "EURUSD",
        timeframe: str = "M5",
        spread_pips: Optional[float] = None,
        dt: Optional[datetime] = None,
    ) -> ScalpDecision:

        now = (dt or datetime.now(timezone.utc)).isoformat()
        atr = calculate_atr(candles)
        block_reasons: list[str] = []

        # ── Layer 0: AMD Phase ────────────────────────────────────────────────
        amd = analyze_amd(candles, current_price)

        # ── Layer 1: SMC Signal ───────────────────────────────────────────────
        smc = decide(
            candles=candles,
            structure=structure,
            liquidity=liquidity,
            displacement=displacement,
            retracement=retracement,
            current_price=current_price,
            amd=amd,
            min_rr=self.min_rr,
            min_confidence=self.min_confidence,
            symbol=symbol,
        )

        if smc.signal == Signal.WAIT:
            return ScalpDecision(
                symbol=symbol,
                timeframe=timeframe,
                evaluated_at=now,
                signal=smc,
                amd=amd,
                scalp_filter=_empty_filter(),
                sizing=_empty_sizing(),
                ready_to_trade=False,
                block_reasons=["SMC signal = WAIT"] + smc.warnings,
                summary=f"No SMC edge — {amd.phase.value} phase. {amd.notes[-1] if amd.notes else ''}",
            )

        # ── Layer 2: Execution Filter ─────────────────────────────────────────
        bias_str = "bullish" if smc.signal == Signal.BUY else "bearish"
        flt = run_scalp_filter(
            candles=candles,
            bias=bias_str,
            atr=atr,
            current_price=current_price,
            spread_pips=spread_pips,
            dt=dt,
            max_spread_pips=self.max_spread_pips,
            min_atr_pct=self.min_atr_pct,
            max_atr_pct=self.max_atr_pct,
            min_liquidity_score=self.min_liquidity_score,
        )

        if not flt.ok:
            block_reasons += [f"ExecFilter: {r}" for r in flt.failed_checks]

        # ── Layer 3: Risk Manager ─────────────────────────────────────────────
        entry_mid = (smc.entry_zone[0] + smc.entry_zone[1]) / 2
        sizing = self.risk_mgr.size_trade(
            entry=entry_mid,
            sl=smc.stop_loss,
            symbol=symbol,
            direction=smc.signal.value,
        )

        if not sizing.allowed:
            block_reasons.append(f"RiskMgr: {sizing.reason}")

        ready = len(block_reasons) == 0

        pip_size = 0.0001 if "JPY" not in symbol.upper() else 0.01
        half_spread = ((spread_pips or 0) * pip_size) / 2

        if smc.signal == Signal.BUY:
            exec_entry = round(current_price + half_spread, 5)
        else:
            exec_entry = round(current_price - half_spread, 5)

        exec_sl = sizing.sl_adjusted
        exec_tp = smc.take_profit

        summary = _build_summary(ready, smc, flt, sizing, block_reasons, amd)

        return ScalpDecision(
            symbol=symbol,
            timeframe=timeframe,
            evaluated_at=now,
            signal=smc,
            amd=amd,
            scalp_filter=flt,
            sizing=sizing,
            ready_to_trade=ready,
            block_reasons=block_reasons,
            summary=summary,
            exec_entry=exec_entry,
            exec_sl=exec_sl,
            exec_tp=exec_tp,
        )

    def confirm_entry(self, trade_id: str, entry: float) -> None:
        self.risk_mgr.record_trade_open(trade_id, entry)

    def close_trade(self, trade_id: str, pnl: float) -> None:
        self.risk_mgr.record_trade_close(trade_id, pnl)

    def risk_status(self) -> dict:
        return self.risk_mgr.status()

    def reset_day(self, new_balance: Optional[float] = None) -> None:
        self.risk_mgr.reset(new_balance)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _empty_filter() -> ScalpFilterResult:
    return ScalpFilterResult(
        ok=False, failed_checks=["not_evaluated"], passed_checks=[],
        spread_pips=None, atr_pct=0, session="unknown", momentum_score=0,
    )


def _empty_sizing() -> SizingResult:
    return SizingResult(
        allowed=False, lots=0, units=0,
        risk_amount=0, risk_pct=0,
        sl_pips=0, sl_adjusted=0,
        reason="not_evaluated",
    )


def _build_summary(ready, smc, flt, sizing, blocks, amd: AMDResult) -> str:
    phase_emoji = {
        "ACCUMULATION": "🔄",
        "MANIPULATION": "⚡",
        "DISTRIBUTION": "🎯",
        "UNKNOWN": "❓",
    }.get(amd.phase.value, "❓")

    if ready:
        return (
            f"✅ READY [{smc.setup_type}] — {smc.signal.value} | "
            f"{phase_emoji} AMD:{amd.phase.value} | "
            f"Conf:{smc.confidence:.0%} | "
            f"R:R {smc.rr_ratio:.1f} | "
            f"Lots:{sizing.lots} | "
            f"Risk:${sizing.risk_amount} | "
            f"{flt.session}"
        )
    else:
        top = blocks[0] if blocks else "unknown"
        return (
            f"⛔ BLOCKED [{smc.setup_type or 'NO_SETUP'}] — {top} "
            f"| {phase_emoji} AMD:{amd.phase.value} "
            f"({len(blocks)} issue{'s' if len(blocks) > 1 else ''})"
        )