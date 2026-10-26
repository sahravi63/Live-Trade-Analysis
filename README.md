# SMC Trading Engine v6 — now with an automated Market Analysis Bot

## Quick start (Windows)

1. Double-click **`setup.bat`** once (installs Python + dashboard packages).
2. Open MetaTrader 5 and log in (optional — without it the bot uses free Yahoo Finance data).
3. Either
   - double-click **`start_all.bat`** → API + dashboard open at http://localhost:5173 → press **▶ Start bot** in the Market Bot panel, or
   - double-click **`start_bot.bat`** to run the bot alone in a terminal.

Run only one of the two bots at a time (a lock file stops a second one from starting).

## What the bot does

Every `scan_interval_seconds` (default 60), for each symbol × timeframe in `config.json`:

```
fetch candles      MT5 terminal → Yahoo Finance (free) → error          data_connector.py
SMC + AMD signal   structure, liquidity, FVG/OTE, AMD phase             decision_engine.py
exec filter        spread, volatility, session, momentum, doji, volume  scalp_filter.py
risk manager       0.5% risk sizing, 2% daily halt, cooldown, max open  risk_manager.py
market sanity      price in entry zone, SL/TP on the right side, R:R ok bot.py
news blackout      ±30 min around high-impact releases (Forex Factory)  news_filter.py
Time Machine       5,000 Monte Carlo replays from the pair's own bars   timemachine.py
                   + re-run of the engine on past bars (how did this setup really do?)
→ open trade       paper (default) | alerts-only | mt5-demo              paper_broker.py / mt5_executor.py
→ manage trade     SL / TP / time exit after max_hold_bars
→ alert            console, alerts.log, Windows sound, Telegram          alerts.py
```

The Time Machine idea comes from [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill):
replay the trade before risking money. A trade is skipped if either replay shows a negative edge.

### Commands

```
python bot.py run                         # loop forever
python bot.py once                        # one scan, print the table
python bot.py status                      # paper account, open trades, risk state
python bot.py backtest EURUSD M5 --count 1000
python bot.py reset-paper                 # wipe paper trades
python bot.py test-alert                  # check alert channels
python bot.py --mock once                 # offline, simulated candles
python -m unittest discover -s tests -v   # offline test suite
```

### Output files

| File | What |
|---|---|
| `bot_output/latest_scan.json` | Last scan: every symbol/timeframe, signal, block reasons, Time Machine odds |
| `bot_output/journal.jsonl` | Every open / close / signal event |
| `bot_output/trades.csv` | Closed trades (open in Excel) |
| `bot_output/alerts.log` | Every alert sent |
| `bot_state/paper.json`, `risk.json` | Paper account + today's risk state (survive restarts) |

### Settings (`config.json`)

- `watchlist`, `timeframes`, `scan_interval_seconds`, `data_source` (`auto` / `mt5` / `yahoo` / `mock`)
- `spread.assumed_pips` is used when MT5 can't give a live spread (Yahoo has none); `spread.max_pips` per symbol
- `account` — balance, risk per trade, daily loss halt, max open trades, cooldown
- `timemachine.min_expected_r` — minimum edge (in R) a trade must show in replays
- `execution.mode`
  - `paper` (default): simulated fills, no orders sent anywhere
  - `alerts-only`: just tells you about setups
  - `mt5-demo`: sends market orders with SL/TP to MT5 — **refuses to run unless the logged-in account is a demo account**
- The dashboard's top **Buy** and **Sell** controls open manual paper trades. Both directions are available regardless of the signal; the matching signal uses its analysis levels, while the opposite direction (or WAIT) gets ATR-based levels. Manual trades bypass automatic setup filters but still obey risk sizing, cooldown, max-open, and minimum R:R checks; they never send broker orders.
- The dashboard's **Demo account** panel shows balance, mark-to-market equity, floating P&L, open orders, and recent closures. Deposit and withdrawal controls modify only the persisted virtual paper account; withdrawals cannot exceed realized balance.
- Open paper orders can be closed from the order table. The matching chart shows only concise `SL` and `TP` labels; use the order controls to step those prices up/down and edit lots, then click **Update** to apply the changes. Stop losses can only move toward lower risk, and increased lots must stay within the per-trade risk limit.
- The API monitors open paper orders every 15 seconds even when **Market Bot** is stopped. A new candle that hits SL or TP closes at that level; if one candle touches both, SL is assumed first. Keep the API process running for automatic monitoring.
- Telegram: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`

### API endpoints added

`GET /bot/status` · `POST /bot/start` · `POST /bot/stop` · `POST /bot/scan-once` · `POST /bot/manual-trade` ·
`POST /bot/positions/{position_id}/close` · `PATCH /bot/positions/{position_id}` ·
`POST /bot/paper-account/deposit` · `POST /bot/paper-account/withdraw` · `GET /bot/journal` ·
`POST /bot/reset-paper` · `GET /bot/backtest?symbol=EURUSD&timeframe=M5`

### Fixes made while building the bot

- `risk_manager.py`: `_get_pip_value` was called but never defined (every allowed trade crashed);
  gold used a 0.0001 pip (sizing off by 100×) — now 0.01 with its own SL range; state can be saved/restored.
- `decision_engine.py`: a BUY could be issued with its stop above entry (AMD manipulation stop on the
  wrong side); signals with impossible levels now return WAIT.
- `data_connector.py`: no longer crashes without `.env` credentials; uses `tick_volume` for FX
  (real_volume is 0, so the volume-spike guard never worked); live MT5 spread; Yahoo fallback.

Originals are in `_backup_before_bot/`.

**Paper-trade it for weeks before trusting it. Most short-term traders lose money. Not financial advice.**

---

# Engine reference (v3 notes)

## What's New in v3

### Scalping Execution Filter (`scalp_filter.py`) — NEW
Six independent microstructure checks that must **all pass** before any entry is allowed.
The SMC signal layer tells you *what* to trade; this layer tells you *whether it's safe right now*.

| Check | Logic | Default Threshold |
|---|---|---|
| **Spread filter** | Bid/ask spread must be tight enough to leave edge after cost | ≤ 1.5 pips |
| **Volatility gate** | ATR% must sit in a tradeable band — not dead flat, not news blow-off | 0.025% – 0.30% of price |
| **Session filter** | Scores each session by liquidity; blocks off-hours trading | ≥ 0.55 (Tokyo or better) |
| **Momentum check** | ≥60% of last 5 candles must agree with the trade bias | 60% agreement |
| **Body filter** | Entry candle body must be ≥35% of its range — rejects doji/indecision | ≥ 35% body ratio |
| **Volume spike guard** | Blocks entries immediately after a 3× volume spike (news) | ≤ 3× average volume |

Session liquidity scores: London/NY Overlap 1.0 → London 0.85 → New York 0.80 → Tokyo 0.55 → Sydney 0.40 → Off-Hours 0.15

### Risk Manager (`risk_manager.py`) — NEW
Stateful across the full trading session. Handles everything signal engines ignore.

**Hard blocks (any triggers → order refused):**
- Daily drawdown ≥ 2% → auto-halt, all trading suspended
- Trade cooldown: 60-second minimum between entries
- Max 2 concurrent open positions
- 3 consecutive losses → pause, manual review required
- SL tighter than 3 pips or wider than 20 pips → rejected (scalp guardrails)

**Position sizing:**
- Risks exactly 0.5% of account per trade
- Lot size calculated from actual pip distance to SL — never fixed
- SL price widened by 1 pip spread buffer so broker spread can't immediately trigger it
- JPY pairs handled separately (pip = 0.01 not 0.0001)

### Scalp Orchestrator (`scalp_orchestrator.py`) — NEW
Single callable that runs all three layers in sequence and short-circuits early:

```
SMC Signal → Execution Filter → Risk Manager → ScalpDecision
```

Returns one `ready_to_trade: bool` plus `exec_entry / exec_sl / exec_tp` with spread already baked in. All `block_reasons` are explicit strings — no silent failures.

### API (`api.py`) — Updated to v3
`/analyze` response now includes the full scalping layer output. Three new endpoints for trade lifecycle management.

---

## Project Structure

```
smc_engine/
├── market_structure.py    # HH/HL/LH/LL, BOS, CHoCH, OB, Premium/Discount/EQ zones
├── liquidity.py           # Equal highs/lows detection, stop hunt identification
├── displacement.py        # FVG detection + fill tracking, OTE zone, retracement quality
├── decision_engine.py     # Hard-gated SMC scoring, swing-anchored SL/TP
├── scalp_filter.py        # NEW — 6-check execution readiness gate (spread, vol, session…)
├── risk_manager.py        # NEW — position sizing, daily loss limit, cooldown, halt logic
├── scalp_orchestrator.py  # NEW — combines all 3 layers into one ScalpDecision output
├── data_connector.py      # MT5 live connector + mock OHLCV generator
├── api.py                 # FastAPI backend v3 (adds /trade/open|close, /risk/status)
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

## Execution Flow (v3)

```
[Candles + Price]
      │
      ▼
 SMC Signal Layer          decision_engine.py
 ─────────────────
 Trend alignment?          Hard gate → WAIT if no
 FVG present?              Hard gate → WAIT if no
 Confidence ≥ 45%?         Hard gate → WAIT if no
 R:R ≥ 1.8?                Hard gate → WAIT if no
      │
      ▼ (signal = BUY or SELL)
 Execution Filter          scalp_filter.py
 ─────────────────
 Spread ≤ 1.5 pips?
 Volatility in range?
 Liquid session?
 Momentum agrees?
 No doji candle?
 No volume spike?
      │
      ▼ (all 6 pass)
 Risk Manager              risk_manager.py
 ─────────────────
 Daily loss < 2%?
 Cooldown elapsed?
 Open trades < 2?
 Consec losses < 3?
 SL in 3–20 pip range?
 → Size lots (0.5% risk)
 → Adjust SL for spread
      │
      ▼
 ScalpDecision
 ready_to_trade: bool
 exec_entry / exec_sl / exec_tp
 lots / risk_amount
 block_reasons (if any)
```

---

## SMC Scoring System (max = 100)

### Hard Gates — any failure → WAIT immediately

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
| Stop hunt in signal direction | +15 |
| Price in discount (BUY) / premium (SELL) zone | +10 |
| OTE zone (62–79% Fib) | +10 |
| Weak retracement (<30% into FVG) | +10 |
| Strong displacement (>65% score) | +10 |
| Price inside Order Block | +8 |
| Multi-confluence bonus (≥4 reasons) | +5 |
| Deep retracement (>70%) | −10 |
| FVG mitigated (>50% filled) | −10 |
| BUY in premium / SELL in discount zone | −10 |

---

## API Endpoints

### Analysis
| Endpoint | Description |
|---|---|
| `GET /analyze?symbol=EURUSD&timeframe=M5` | Full SMC + scalp execution analysis |
| `GET /analyze?…&spread_pips=0.8` | Pass live spread for accurate filter + SL adjustment |
| `GET /analyze?…&use_mock=true` | Use simulated candle data |
| `GET /health` | Server health + MT5 status + version |
| `GET /symbols` | Available currency pairs |
| `GET /docs` | Swagger UI |

### Trade Lifecycle (NEW — feeds the risk manager)
| Endpoint | Description |
|---|---|
| `POST /trade/open?trade_id=T1&entry=1.08500` | Notify risk manager of opened trade |
| `POST /trade/close?trade_id=T1&pnl=-15.0` | Record closed trade P&L |
| `GET /risk/status` | Daily P&L, open trades, halt status, drawdown remaining |
| `POST /risk/reset?new_balance=10000` | Reset for a new trading day |

### `/analyze` Response — New Fields in v3

```jsonc
{
  // ... all existing v2 fields ...

  "scalp_filter": {
    "ok": true,
    "passed_checks": ["spread_ok", "volatility_ok", "session_ok", ...],
    "failed_checks": [],
    "spread_pips": 0.8,
    "atr_pct": 0.00031,
    "session": "London/NY Overlap",
    "momentum_score": 0.8,
    "notes": []
  },
  "sizing": {
    "allowed": true,
    "lots": 0.14,
    "units": 14000,
    "risk_amount": 50.0,
    "risk_pct": 0.005,
    "sl_pips": 8.0,
    "sl_adjusted": 1.08420,
    "reason": "OK"
  },
  "ready_to_trade": true,
  "block_reasons": [],
  "exec_entry": 1.08504,
  "exec_sl": 1.08420,
  "exec_tp": 1.08680,
  "scalp_summary": "✅ READY — BUY | Confidence: 72% | R:R 2.2 | Lots: 0.14 | Risk: $50 | Session: London/NY Overlap"
}
```

---

## Configuring the Orchestrator

All thresholds are set in `scalp_orchestrator.py` constructor. Override for your broker/pair:

```python
# In api.py — tune before deployment
_orchestrator = ScalpOrchestrator(
    account_balance=10_000,       # your real account size
    risk_pct=0.005,               # 0.5% per trade — lower for faster pairs
    max_spread_pips=1.5,          # tighten to 0.8 for ECN brokers
    min_atr_pct=0.00025,          # raise for JPY pairs
    max_atr_pct=0.003,            # lower during high-impact news hours
    min_liquidity_score=0.55,     # raise to 0.80 to restrict to London+NY only
    min_rr=1.8,
    min_confidence=0.45,
)
```

Configuring `RiskManager` directly:

```python
rm = RiskManager(
    account_balance=10_000,
    risk_pct_per_trade=0.005,     # 0.5% risk
    max_daily_loss_pct=0.02,      # halt at 2% daily drawdown
    min_cooldown_seconds=60,      # 1 min between trades
    max_open_trades=2,
    max_consecutive_losses=3,
    spread_buffer_pips=1.0,       # SL widening for spread
    min_sl_pips=3.0,
    max_sl_pips=20.0,
)
```

---

## MT5 Connection

1. Open MetaTrader 5 → Tools → Options → Expert Advisors → Allow Algo Trading
2. Keep MT5 running and logged in
3. Run `uvicorn api:app --reload --port 8000` (Python and MT5 must be same bitness)
4. Pass `spread_pips` from your MT5 tick data to `/analyze` for accurate execution levels
5. Toggle **Mock** off in the dashboard

### Getting live spread from MT5 (add to `data_connector.py`)

```python
import MetaTrader5 as mt5

def get_spread_pips(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    spread = tick.ask - tick.bid
    # Convert to pips (4-decimal pairs)
    return round(spread / 0.0001, 1)
```

---

*Not financial advice. SMC analysis only. Always manage your risk.*