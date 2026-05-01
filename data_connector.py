"""
MT5 Data Connector
Configured for: Ravi Sah | MetaQuotes-Demo | Login: 5049729986
"""
import os
from dotenv import load_dotenv
import time
import random
from datetime import datetime
from typing import Optional

# ── Try to import MT5 ─────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# Explicitly load .env from project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# MT5 Credentials
MT5_LOGIN = os.getenv("MT5_LOGIN")
MT5_PASS = os.getenv("MT5_PASS")
MT5_SERVER = os.getenv("MT5_SERVER", "MetaQuotes-Demo")

# Convert login safely
if MT5_LOGIN:
    MT5_LOGIN = int(MT5_LOGIN)

# Optional validation
if not MT5_LOGIN or not MT5_PASS:
    raise ValueError("MT5 credentials are missing in the .env file.")

# ── MT5 timeframe map ─────────────────────────────────────────────────────────
TIMEFRAME_MAP = {
    "M1":  mt5.TIMEFRAME_M1  if MT5_AVAILABLE else 1,
    "M5":  mt5.TIMEFRAME_M5  if MT5_AVAILABLE else 5,
    "M15": mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 15,
    "M30": mt5.TIMEFRAME_M30 if MT5_AVAILABLE else 30,
    "H1":  mt5.TIMEFRAME_H1  if MT5_AVAILABLE else 16385,
    "H4":  mt5.TIMEFRAME_H4  if MT5_AVAILABLE else 16388,
    "D1":  mt5.TIMEFRAME_D1  if MT5_AVAILABLE else 16408,
}

_connected = False


def connect_mt5(
    login: int = MT5_LOGIN,
    password: str = MT5_PASS,
    server: str = MT5_SERVER,
) -> bool:
    global _connected
    if not MT5_AVAILABLE:
        print("MetaTrader5 package not installed.")
        return False

    # Only attach to already-running MT5 terminal (no explicit login)
    if mt5.initialize():
        info = mt5.terminal_info()
        acct = mt5.account_info()
        if info and info.connected and acct:
            print(f"✓ MT5 Attached to running terminal | {acct.name} | {acct.server} | Balance: {acct.balance} {acct.currency}")
            _connected = True
            return True
        mt5.shutdown()
        print("MT5 terminal not connected to broker or not logged in.")
        return False
    else:
        err = mt5.last_error()
        print(f"MT5 initialize() failed: Could not attach to running terminal. Error: {err}")
        print("Check that both Python and MT5 are the same bitness (32/64-bit) and running as the same user (admin or not admin).\nSee https://www.mql5.com/en/docs/integration/python_metatrader5 for troubleshooting.")
        return False


def ensure_connected() -> bool:
    """Auto-reconnect if connection dropped."""
    global _connected
    if not MT5_AVAILABLE:
        return False
    try:
        info = mt5.terminal_info()
        if info is not None and info.connected:
            return True
    except Exception:
        pass
    print("MT5 disconnected — reconnecting...")
    _connected = False
    return connect_mt5()


def disconnect_mt5():
    global _connected
    if MT5_AVAILABLE:
        mt5.shutdown()
    _connected = False


def fetch_candles_mt5(symbol: str, timeframe: str = "H1", count: int = 200) -> list[dict]:
    """Fetch live candles from MT5."""
    if not ensure_connected():
        raise RuntimeError("MT5 not connected.")

    tf = TIMEFRAME_MAP.get(timeframe)
    if tf is None:
        raise RuntimeError(f"Unknown timeframe: {timeframe}")

    # Make sure symbol is available in Market Watch
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbol {symbol} not found in Market Watch.")

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        raise RuntimeError(f"MT5 returned no data for {symbol}. Error: {err}")

    return [
        {
            "time":   datetime.fromtimestamp(r["time"]).isoformat(),
            "open":   float(r["open"]),
            "high":   float(r["high"]),
            "low":    float(r["low"]),
            "close":  float(r["close"]),
            "volume": int(r["real_volume"]),
        }
        for r in rates
    ]


# ── Mock data generator ───────────────────────────────────────────────────────

def generate_mock_candles(
    n: int = 200,
    base_price: float = 1.08500,
    volatility: float = 0.0015,
    trend_bias: float = 0.0001,
) -> list[dict]:
    candles = []
    price = base_price
    now = int(time.time()) - n * 3600

    for i in range(n):
        is_displacement = random.random() < 0.06
        size = volatility * (random.uniform(2.5, 4.0) if is_displacement else random.uniform(0.3, 1.2))
        direction = 1 if random.random() > 0.48 else -1
        open_p  = price
        close_p = price + direction * size + trend_bias
        high_p  = max(open_p, close_p) + random.uniform(0, size * 0.4)
        low_p   = min(open_p, close_p) - random.uniform(0, size * 0.4)

        if i > 10 and random.random() < 0.08:
            prev_high = candles[-random.randint(2, 8)]["high"]
            high_p = prev_high + random.uniform(-0.00005, 0.00005)

        candles.append({
            "time":   datetime.fromtimestamp(now + i * 3600).isoformat(),
            "open":   round(open_p, 5),
            "high":   round(high_p, 5),
            "low":    round(low_p, 5),
            "close":  round(close_p, 5),
            "volume": random.randint(500, 5000),
        })
        price = close_p

    return candles


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_candles(
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    count: int = 200,
    use_mock: bool = False,
) -> list[dict]:
    if not use_mock and MT5_AVAILABLE:
        return fetch_candles_mt5(symbol, timeframe, count)
    return generate_mock_candles(count)


# ── Startup auto-connect ──────────────────────────────────────────────────────
# Runs automatically when api.py imports this module
if MT5_AVAILABLE:
    connect_mt5()