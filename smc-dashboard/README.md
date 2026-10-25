# SMC Trading Engine v4.0

A full-stack Smart Money Concepts (SMC) trading analysis platform built with a **FastAPI** Python backend and a **React + Vite** frontend. Connects live to **MetaTrader 5** for real-time OHLCV data and runs a five-layer pipeline — from structural analysis through AMD phase detection, confluence scoring, microstructure filtering, and position sizing — before producing a single `ready_to_trade` verdict with exact execution levels.

> **Not financial advice. SMC analysis only. Always manage your own risk.**

---

## What this system does

The engine answers one question on every request: **"Is there an SMC edge right now, and is it safe to enter?"**

It does this by running five sequential layers, each of which can veto the trade before the next layer runs:

```
Market data (MT5 or mock)
        │
        ▼
SMC Analysis ──────────── market_structure · liquidity · displacement · amd_phases
        │
        ▼
Decision Engine ─────────  Confluence score → BUY / SELL / WAIT
        │  (WAIT exits here)
        ▼
Execution Filter ─────────  6 microstructure checks (spread, vol, session, momentum…)
        │  (any failure exits here)
        ▼
Risk Manager ─────────────  Daily loss · cooldown · open cap · lot sizing
        │  (any block exits here)
        ▼
ScalpDecision ────────────  ready_to_trade + exec_entry / exec_sl / exec_tp
```

---

## Project structure

```
smc_engine/
├── market_structure.py    # Swing detection, HH/HL/LH/LL, BOS, CHoCH, Order Blocks, PDZ zones
├── liquidity.py           # Equal highs/lows detection, stop hunt identification
├── displacement.py        # FVG detection + fill tracking, OTE (62–79% Fib), retracement quality
├── amd_phases.py          # AMD phase engine v2 — Accumulation / Manipulation / Distribution
├── decision_engine.py     # Confluence scoring (max ~120) → BUY / SELL / WAIT
├── scalp_filter.py        # 6-check execution readiness gate
├── risk_manager.py        # Position sizing, daily loss limit, cooldown, halt logic
├── scalp_orchestrator.py  # Combines all layers into one ScalpDecision output
├── data_connector.py      # MT5 live connector + mock OHLCV generator
├── api.py                 # FastAPI backend v5
├── requirements.txt
└── dashboard/
    └── SMCDashboard.jsx   # React frontend v4.0
```

---

## Backend setup

### Requirements

- Python 3.11+
- MetaTrader 5 desktop terminal (Windows only for live data; mock mode works on any OS)
- Python and MT5 must be the **same bitness** (both 64-bit)

```bash
pip install -r requirements.txt
uvicorn api:app --reload --port 8000
```

API docs (Swagger UI): http://localhost:8000/docs

### MT5 connection

1. Open MetaTrader 5 → Tools → Options → Expert Advisors → **Allow Algo Trading**
2. Keep MT5 running and logged in to your broker account
3. Start the backend — it will auto-attach to the running terminal on import
4. Pass `?spread_pips=` on every live request so the execution filter and SL adjustment work correctly

Getting live spread from MT5 (add to `data_connector.py` if needed):

```python
import MetaTrader5 as mt5

def get_spread_pips(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return round((tick.ask - tick.bid) / 0.0001, 1)
```

> **Multi-worker warning:** `_orchestrator` in `api.py` is a module-level singleton. Run with a single Uvicorn worker only (`uvicorn api:app --port 8000`). For multi-worker or gunicorn deployments, move `RiskState` to Redis before scaling.

---

## Frontend setup

### Requirements

- Node.js 18+
- Vite + React (template bootstrapped — see below)
- `lightweight-charts` for candlestick rendering

```bash
npm create vite@latest smc-dashboard -- --template react
cd smc-dashboard
npm install lightweight-charts
```

Copy `dashboard/SMCDashboard.jsx` → `src/SMCDashboard.jsx`, then update `src/App.jsx`:

```jsx
import SMCDashboard from './SMCDashboard'
export default function App() { return <SMCDashboard /> }
```

```bash
npm run dev   # http://localhost:5173
```

The dashboard connects to `http://localhost:8000` by default. Change the `API` constant at the top of `SMCDashboard.jsx` if your backend runs on a different host/port.

---

## SMC analysis modules

### market_structure.py

Detects pivot-based swing highs and lows with an alternation filter (no double-tops noise). Labels each swing: **HH / HL / LH / LL**. Confirms:

- **BOS** (Break of Structure) — only on candle close beyond prior swing level, not a wick
- **CHoCH** (Change of Character) — requires at least one confirmed opposing structure label
- **Order Blocks** — last opposing-colour candle before a BOS impulse
- **Premium / Discount / Equilibrium zones** — 75% / 25% / 50% of the last major impulse leg

### liquidity.py

Clusters highs and lows within `tolerance_pips` (default 0.3 pips) to find **equal highs** (buy-side liquidity) and **equal lows** (sell-side liquidity). Detects **stop hunts** — wicks through a level that close back inside. Returns the nearest unswept level above and below current price.

### displacement.py

Three-candle **FVG** (Fair Value Gap) detection. Middle candle body must be ≥ 1× ATR for a valid impulse. Tracks fill status:

- `filled` — price has fully traded through the gap
- `mitigated` — more than 50% filled (reliability reduced)

Strength score: 65% body-size ratio + 35% directional streak.

**OTE zone** — 62–79% Fibonacci retracement of the last impulse leg, used as the optimal entry window.

### amd_phases.py (v2)

Classifies the current market into one of three institutional phases:

| Phase | Meaning |
|---|---|
| **Accumulation** | Price ranging — institutions building a position |
| **Manipulation** | Stop hunt beyond the range boundary — engineered liquidity grab |
| **Distribution** | Directional move following the hunt — the real delivery |

v2 additions:
- **MSS (Market Structure Shift)** — first Lower High after a bearish hunt (or first Higher Low after bullish) confirms the structural flip. Distribution score is reduced from +20 to +12 without MSS confirmation.
- **Momentum divergence** — price makes new extreme but close-delta momentum does not → trapped-money signal (+8 confluence bonus)
- **Entry zone tightened** to the FVG inside the manipulation leg when one is present
- **Oscillator data** (close-delta series) exported for the dashboard chart panel

Confidence scoring: `base (0.45 / 0.65) + MSS bonus (0.12) + divergence bonus (0.08) + FVG bonus (0.05)`, capped at 0.95.

---

## Decision engine (confluence scoring)

### Hard gates — any failure → WAIT immediately

| Condition | Result |
|---|---|
| No clear trend structure | WAIT |
| Trend ≠ displacement direction (unless CHoCH reversal) | WAIT |
| SELL in discount zone / BUY in premium zone | WAIT |
| No active (unfilled) FVG near price | WAIT |
| Confidence < 0.50 after scoring | WAIT |
| R:R < 1.8 | WAIT |

### Confluence score (max ≈ 120)

| Condition | Points |
|---|---|
| BOS confirmed in signal direction | +25 |
| CHoCH with matching displacement | +20 |
| AMD Distribution phase + MSS confirmed | +20 |
| AMD Manipulation phase, matching bias | +15 |
| Price at / near LH (SELL) or HL (BUY) | +15 |
| Price inside valid FVG (direction matches) | +20 |
| Stop hunt in signal direction | +15 |
| Price in discount (BUY) / premium (SELL) zone | +10 |
| AMD entry zone confluence | +10 |
| OTE zone (62–79% Fib) | +10 |
| Clean pullback — weak retracement (<30%) | +10 |
| Strong displacement (≥65% score) | +10 |
| Price inside Order Block | +8 |
| Momentum divergence detected | +8 |
| Multi-confluence bonus (≥4 reasons) | +5 |
| **Penalties** | |
| Deep retracement (>70%) | −10 |
| FVG mitigated (>50% filled) | −10 |
| AMD Distribution, no MSS yet | −8 (score capped to +12 instead of +20) |
| AMD Accumulation — no manipulation yet | −5 |

Confidence = `min(score / 100, 1.0)`. Minimum threshold: **0.50**.

---

## Execution filter (scalp_filter.py)

Six independent checks — **all must pass**:

| Check | Logic | Default threshold |
|---|---|---|
| **Spread** | Bid/ask spread ≤ max | ≤ 1.5 pips |
| **Volatility** | ATR% in tradeable band | 0.025% – 0.30% of price |
| **Session** | Liquidity score ≥ minimum | ≥ 0.55 (Tokyo or better) |
| **Momentum** | ≥60% of last 5 candles agree with bias | 60% |
| **Candle body** | Entry candle body ≥ 35% of range (rejects doji) | 35% |
| **Volume spike** | Last candle volume ≤ 3× rolling average | ≤ 3× avg |

Session liquidity scores:

| Session | Score | UTC hours |
|---|---|---|
| London / NY Overlap | 1.00 | 12:00 – 16:00 |
| London | 0.85 | 07:00 – 16:00 |
| New York | 0.80 | 12:00 – 21:00 |
| Tokyo | 0.55 | 00:00 – 09:00 |
| Sydney | 0.40 | 21:00 – 06:00 (non-overlapping) |
| Off-Hours | 0.15 | all other times |

> If `spread_pips` is not passed, the spread check returns `spread_unknown` and **blocks** the trade. Always provide live spread for production use.

---

## Risk manager (risk_manager.py)

Stateful across the trading session. Hard blocks — any triggers → order refused:

| Rule | Default |
|---|---|
| Daily drawdown ≥ 2% | Trading halted for the day |
| Trade cooldown | 60 seconds minimum between entries |
| Max open positions | 2 concurrent trades |
| Consecutive losses | 3 in a row → pause, manual review |
| SL too tight | < 3 pips rejected |
| SL too wide | > 20 pips rejected |

**Position sizing:** Risks exactly 0.5% of account balance per trade. Lot size = `dollar_risk / (effective_sl_pips × pip_value)`. SL is widened by 1 pip spread buffer so the broker spread cannot immediately trigger it. JPY pairs use pip = 0.01; all others use 0.0001. Gold (XAUUSD) uses pip_value = $1/lot.

---

## API endpoints

### Analysis

| Endpoint | Description |
|---|---|
| `GET /analyze` | Full SMC + AMD + scalp execution analysis |
| `GET /analyze?symbol=EURUSD&timeframe=M5` | Specify pair and timeframe |
| `GET /analyze?spread_pips=0.8` | Pass live spread for execution filter and SL adjustment |
| `GET /analyze?use_mock=true` | Use generated OHLCV data (no MT5 required) |
| `GET /analyze?momentum_period=14` | AMD/oscillator momentum lookback (default 14) |
| `GET /health` | Server health, MT5 status, version |
| `GET /symbols` | Available currency pairs |
| `GET /docs` | Swagger UI |

### Trade lifecycle

| Endpoint | Description |
|---|---|
| `POST /trade/open?trade_id=T1&entry=1.08500` | Notify risk manager of opened trade |
| `POST /trade/close?trade_id=T1&pnl=-15.0` | Record closed trade P&L |
| `GET /risk/status` | Daily P&L, open trades, halt status, drawdown remaining |
| `POST /risk/reset?new_balance=10000` | Reset session for a new trading day |

### Supported symbols

`EURUSD · GBPUSD · USDJPY · USDCHF · AUDUSD · NZDUSD · USDCAD · GBPJPY · EURJPY · EURGBP · XAUUSD`

### Supported timeframes

`M1 · M5 · M15 · M30 · H1 · H4 · D1`

---

## `/analyze` response — key fields

```jsonc
{
  "symbol": "EURUSD",
  "timeframe": "M5",
  "current_price": 1.08524,

  // SMC structure
  "trend": "BEARISH",
  "last_event": "BOS",
  "bos_level": 1.08750,
  "swing_highs": [...],
  "swing_lows": [...],
  "order_blocks": [...],
  "premium_zone": 1.08900,
  "discount_zone": 1.08200,
  "equilibrium": 1.08550,

  // Liquidity
  "liquidity_levels": [...],
  "stop_hunts": [...],
  "nearest_buy_side": 1.09100,
  "nearest_sell_side": 1.08050,

  // Displacement / FVG
  "fvgs": [...],
  "displacement_direction": "bearish",
  "displacement_strength": 0.74,
  "ote_low": 1.08410,
  "ote_high": 1.08530,

  // Retracement
  "is_retracing": true,
  "retracement_pct": 42.5,
  "inside_fvg": true,
  "inside_ote": false,
  "retracement_strength": "moderate",

  // Signal
  "signal": "SELL",
  "confidence": 0.72,
  "entry_zone": [1.08490, 1.08560],
  "stop_loss": 1.08780,
  "take_profit": 1.07940,
  "rr_ratio": 2.1,
  "confluence_score": 72,
  "setup_type": "LH_SELL",
  "reasons": [...],
  "warnings": [...],

  // AMD phase
  "amd": {
    "phase": "DISTRIBUTION",
    "bias": "bearish",
    "phase_confidence": 0.85,
    "mss_detected": true,
    "mss_level": 1.08620,
    "divergence_detected": true,
    "divergence_indices": [182, 196],
    "entry_zone_low": 1.08490,
    "entry_zone_high": 1.08555,
    "manipulation_sl": 1.08810,
    "notes": [...]
  },

  // Execution layer
  "scalp_filter": {
    "ok": true,
    "passed_checks": ["spread_ok", "volatility_ok", "session_ok", "momentum_ok (80%)", "body_ok (62%)", "volume_ok"],
    "failed_checks": [],
    "spread_pips": 0.8,
    "atr_pct": 0.00031,
    "session": "London/NY Overlap",
    "momentum_score": 0.8
  },
  "sizing": {
    "allowed": true,
    "lots": 0.14,
    "units": 14000,
    "risk_amount": 50.0,
    "risk_pct": 0.005,
    "sl_pips": 8.0,
    "sl_adjusted": 1.08788
  },
  "ready_to_trade": true,
  "block_reasons": [],
  "exec_entry": 1.08528,
  "exec_sl": 1.08788,
  "exec_tp": 1.07940,
  "scalp_summary": "✅ READY [LH_SELL] — SELL | 🎯 AMD:DISTRIBUTION | Conf:72% | R:R 2.1 | Lots:0.14 | Risk:$50 | London/NY Overlap",

  // Chart data
  "oscillator_data": [...],   // close-delta momentum series for the chart panel
  "candles": [...],           // last 200 OHLCV bars
  "mt5_connected": true
}
```

---

## Dashboard (SMCDashboard.jsx v4.0)

React component using `lightweight-charts` for candlestick rendering. Features:

- **Candlestick chart** with overlaid FVG zones, Order Block zones, OTE zone, BOS/CHoCH levels, MSS horizontal line (purple), and divergence candle markers
- **AMD phase card** — phase badge, bias, confidence bar, MSS level, divergence index range, entry zone, manipulation SL
- **Signal panel** — entry zone, SL, TP, R:R, setup type, confidence, confluence score
- **Scalp execution panel** — ready-to-trade verdict, lot size, risk amount, block reasons
- **Detail tabs** — Confluence reasons · Execution filter checks · Warnings · FVG table · Stop hunts · Order blocks
- **Oscillator panel** — real close-delta series from the API (falls back to client-side for mock mode)
- Supports all 11 symbols and all 7 timeframes with auto-refresh

---

## Configuration

All thresholds are set in the `ScalpOrchestrator` constructor in `api.py`. Override before deployment:

```python
_orchestrator = ScalpOrchestrator(
    account_balance=10_000,       # your real account size
    risk_pct=0.005,               # 0.5% per trade
    max_spread_pips=1.5,          # tighten to 0.8 for ECN brokers
    min_atr_pct=0.00025,          # raise for JPY/XAU pairs
    max_atr_pct=0.003,            # lower during high-impact news
    min_liquidity_score=0.55,     # raise to 0.80 to restrict to London+NY only
    min_rr=1.8,
    min_confidence=0.45,
)
```

RiskManager directly:

```python
rm = RiskManager(
    account_balance=10_000,
    risk_pct_per_trade=0.005,
    max_daily_loss_pct=0.02,      # halt at 2% drawdown
    min_cooldown_seconds=60,
    max_open_trades=2,
    max_consecutive_losses=3,
    spread_buffer_pips=1.0,
    min_sl_pips=3.0,
    max_sl_pips=20.0,
)
```

---

## Known limitations

- `_orchestrator` is a single-process in-memory singleton — do not use `--workers N` without moving `RiskState` to Redis or a database first
- Pip value table in `risk_manager.py` uses hardcoded approximations for USDCHF, USDCAD, JPY, and XAUUSD — lot sizing will drift slightly when exchange rates move; replace with live tick values from `mt5.symbol_info()` for production accuracy
- `_find_consolidation` in `amd_phases.py` is O(lookback²) — the default lookback of 40 (~780 iterations) is fine; do not raise above 60 without switching to a sliding-window min/max approach
- Mock OHLCV generator produces synthetic data without realistic session structure or news spikes — use only for UI testing, not strategy validation

---

*SMC Engine v4.0 · Backend: FastAPI + Python · Frontend: React + Vite + lightweight-charts · Data: MetaTrader 5*