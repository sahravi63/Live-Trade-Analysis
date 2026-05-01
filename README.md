# SMC Trading Engine v2 — Setup Guide

## What's New in v2

### Rule Engine (decision_engine.py)
- **Hard gates**: signal fires ONLY when trend + displacement align — no more false signals from one-sided confluence
- **Premium/Discount zone filter**: BUY signals suppressed in premium; SELL signals suppressed in discount
- **Stop Loss**: anchored to the actual most-recent swing low/high (± 0.25× ATR buffer) — not a fixed ATR multiple
- **Take Profit**: targets the nearest unswept liquidity pool (EQH for BUY, EQL for SELL) — falls back to 1.8R
- **Minimum R:R raised**: from 2.0 to 1.8 (configurable), with confidence threshold at 45%
- **Order Block confluence**: +8 pts when price re-enters a valid OB

### Displacement (displacement.py)
- FVGs require middle candle body ≥ 1.0× ATR — removes weak imbalances
- FVG fill tracking: `filled` (100% filled), `mitigated` (50%+ filled, score penalty)
- Direction uses last 5 FVGs (rolling vote) instead of just the last one
- **OTE zone**: 62–79% Fibonacci retracement of the last impulse leg, used as tight entry window

### Market Structure (market_structure.py)
- **Swing detection**: lookback increased to 5 (was 3) — cleaner structure
- **Alternation enforced**: no duplicate swing highs/lows in quick succession
- **BOS**: confirmed on close through prior swing (not just a wick)
- **CHoCH**: requires displacement to flip direction, not just structure label
- **Order Blocks**: automatically detected (last opposing candle before BOS impulse)
- **Premium / Discount / Equilibrium**: 75% / 25% / 50% of the last impulse leg

### Dashboard (SMCDashboard.jsx)
- **Order Block bands** overlaid (teal = bullish OB, orange = bearish OB)
- **OTE zone** overlaid (purple band, 62–79% Fib)
- **Premium / EQ / Discount** horizontal lines
- Entry zone shown as a **band** (entry_low to entry_high), not just a midpoint
- SL/TP lines with pip distance labels
- FVG table with strength %, filled/mitigated status
- Mitigated FVGs shown as dotted (vs dashed for fresh)

---

## Project Structure

```
smc_engine/
├── market_structure.py    # HH/HL/LH/LL, BOS, CHoCH, OB, PDZ
├── liquidity.py           # Equal highs/lows, stop hunts
├── displacement.py        # FVG (filtered), OTE zone, retracement
├── decision_engine.py     # Hard-gated scoring + swing-level SL/TP
├── data_connector.py      # MT5 connector + mock data
├── api.py                 # FastAPI backend
├── requirements.txt
└── dashboard/
    └── SMCDashboard.jsx   # React frontend v2
```

---

## Backend Setup

```bash
pip install -r requirements.txt
uvicorn api:app --reload --port 8000
```

API docs: http://localhost:8000/docs

---

## Frontend Setup

```bash
npm create vite@latest smc-dashboard -- --template react
cd smc-dashboard
npm install lightweight-charts
```

Copy `dashboard/SMCDashboard.jsx` → `src/SMCDashboard.jsx`

```jsx
// src/App.jsx
import SMCDashboard from './SMCDashboard'
export default function App() { return <SMCDashboard /> }
```

```bash
npm run dev  # http://localhost:5173
```

---

## Scoring System v2 (max = 100)

### Hard Gates (any failure → WAIT immediately)
| Condition | Result |
|---|---|
| Trend ≠ displacement direction | WAIT |
| No active FVG near price | WAIT |
| Confidence < 45% | WAIT |
| R:R < 1.8 | WAIT |

### Confidence Scoring
| Condition | Score |
|---|---|
| BOS confirmed in signal direction | +25 |
| CHoCH with matching displacement | +20 |
| Price inside valid FVG (direction matches) | +20 |
| Price in discount (BUY) / premium (SELL) zone | +10 |
| OTE zone (62–79% Fib) | +10 |
| Weak retracement (<30% into FVG) | +10 |
| Stop hunt in signal direction | +15 |
| Strong displacement (>65% score) | +10 |
| Price inside Order Block | +8 |
| Multi-confluence bonus (≥4 reasons) | +5 |
| Deep retracement (>70%) | −10 |
| FVG mitigated (>50% filled) | −10 |
| BUY in premium / SELL in discount zone | −10 |

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /analyze?symbol=EURUSD&timeframe=H1` | Full SMC analysis |
| `GET /health` | Server health + MT5 status |
| `GET /symbols` | Available pairs |
| `GET /docs` | Swagger UI |

Add `&use_mock=true` for simulated data.

---

## MT5 Connection

1. Open MetaTrader 5 → Tools → Options → Expert Advisors → Allow Algo Trading
2. Keep MT5 running and logged in
3. Run `uvicorn api:app --reload --port 8000` (Python and MT5 must be same bitness)
4. Toggle **Mock** off in the dashboard

---

*Not financial advice. SMC analysis only. Always manage your risk.*