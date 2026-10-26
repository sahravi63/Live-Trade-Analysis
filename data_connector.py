"""
Data Connector — v2 (bot-ready)
================================
One function, three sources, automatic fallback:

    fetch_candles(symbol, timeframe, count, use_mock=False, source="auto")

  source="mt5"    MetaTrader 5 terminal that is already running + logged in
  source="yahoo"  Free Yahoo Finance chart endpoint (no key; unofficial, may lag)
  source="mock"   Random-walk generator for offline testing
  source="auto"   MT5 if attached, else Yahoo, else raise

Changes over v1:
  * No longer raises at import when .env credentials are missing (mock / Yahoo
    work on any machine). python-dotenv is optional.
  * MT5 forex volume: real_volume is 0 for most FX symbols, so tick_volume is
    used instead (the volume-spike guard was reading all zeros before).
  * get_spread_pips(symbol) reads the live MT5 spread for the execution filter.
  * Yahoo fallback with H4 built from 1h bars.
  * last_source() reports which source served the most recent call.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

# ── Optional .env loading ─────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:  # dotenv not installed: read the file by hand
    _env = os.path.join(BASE_DIR, ".env")
    if os.path.exists(_env):
        with open(_env, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ── Optional MT5 import ───────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False

MT5_LOGIN = os.getenv("MT5_LOGIN")
MT5_PASS = os.getenv("MT5_PASS")
MT5_SERVER = os.getenv("MT5_SERVER", "MetaQuotes-Demo")
try:
    MT5_LOGIN = int(MT5_LOGIN) if MT5_LOGIN else None
except ValueError:
    MT5_LOGIN = None

TIMEFRAME_MAP = {
    "M1":  mt5.TIMEFRAME_M1  if MT5_AVAILABLE else 1,
    "M5":  mt5.TIMEFRAME_M5  if MT5_AVAILABLE else 5,
    "M15": mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 15,
    "M30": mt5.TIMEFRAME_M30 if MT5_AVAILABLE else 30,
    "H1":  mt5.TIMEFRAME_H1  if MT5_AVAILABLE else 16385,
    "H4":  mt5.TIMEFRAME_H4  if MT5_AVAILABLE else 16388,
    "D1":  mt5.TIMEFRAME_D1  if MT5_AVAILABLE else 16408,
}
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
              "H1": 3600, "H4": 14400, "D1": 86400}

_connected = False
_last_source = "none"


def last_source() -> str:
    return _last_source


# ══════════════════════════════════════════════════════════════════════════════
# MT5
# ══════════════════════════════════════════════════════════════════════════════

def connect_mt5(login: Optional[int] = MT5_LOGIN,
                password: Optional[str] = MT5_PASS,
                server: str = MT5_SERVER) -> bool:
    """Attach to the running MT5 terminal. Falls back to explicit login if
    credentials are present and plain attach fails."""
    global _connected
    if not MT5_AVAILABLE:
        return False
    ok = mt5.initialize()
    if not ok and login and password:
        ok = mt5.initialize(login=login, password=password, server=server)
    if ok:
        info, acct = mt5.terminal_info(), mt5.account_info()
        if info and info.connected and acct:
            print(f"[data] MT5 attached | {acct.name} | {acct.server} | "
                  f"Balance {acct.balance} {acct.currency}")
            _connected = True
            return True
        mt5.shutdown()
        print("[data] MT5 terminal is open but not logged in to a broker.")
        return False
    print(f"[data] MT5 initialize() failed: {mt5.last_error()} — is the terminal running?")
    return False


def ensure_connected() -> bool:
    global _connected
    if not MT5_AVAILABLE:
        return False
    try:
        info = mt5.terminal_info()
        if info is not None and info.connected:
            _connected = True
            return True
    except Exception:
        pass
    _connected = False
    return connect_mt5()


def mt5_connected() -> bool:
    return MT5_AVAILABLE and _connected


def disconnect_mt5():
    global _connected
    if MT5_AVAILABLE:
        mt5.shutdown()
    _connected = False


def fetch_candles_mt5(symbol: str, timeframe: str = "H1", count: int = 200) -> list[dict]:
    if not ensure_connected():
        raise RuntimeError("MT5 not connected.")
    tf = TIMEFRAME_MAP.get(timeframe)
    if tf is None:
        raise RuntimeError(f"Unknown timeframe: {timeframe}")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Symbol {symbol} not found in Market Watch.")
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"MT5 returned no data for {symbol}: {mt5.last_error()}")
    out = []
    for r in rates:
        vol = int(r["real_volume"]) or int(r["tick_volume"])
        out.append({
            "time":   datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(),
            "open":   float(r["open"]), "high": float(r["high"]),
            "low":    float(r["low"]),  "close": float(r["close"]),
            "volume": vol,
        })
    return out


def get_spread_pips(symbol: str) -> Optional[float]:
    """Live bid/ask spread in pips from MT5, or None if MT5 is unavailable."""
    if not (MT5_AVAILABLE and ensure_connected()):
        return None
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not tick.ask or not tick.bid:
        return None
    return round((tick.ask - tick.bid) / pip_size(symbol), 1)


# ══════════════════════════════════════════════════════════════════════════════
# Yahoo Finance (free, no key)
# ══════════════════════════════════════════════════════════════════════════════

YAHOO_SYMBOLS = {
    "XAUUSD": "GC=F", "XAGUSD": "SI=F",
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
    "USOIL": "CL=F",
    "USTECH": "^NDX", "NAS100": "^NDX",
    "US30": "^DJI", "SPX500": "^GSPC", "UK100": "^FTSE",
}
YAHOO_TF = {  # timeframe -> (interval, range, aggregate_factor)
    "M1": ("1m", "5d", 1), "M5": ("5m", "30d", 1), "M15": ("15m", "30d", 1),
    "M30": ("30m", "30d", 1), "H1": ("60m", "180d", 1), "H4": ("60m", "365d", 4),
    "D1": ("1d", "2y", 1),
}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) market-analysis-bot/1.0"


def yahoo_ticker(symbol: str) -> str:
    s = symbol.upper()
    if s in YAHOO_SYMBOLS:
        return YAHOO_SYMBOLS[s]
    if len(s) == 6 and s.isalpha():
        return f"{s}=X"           # forex pair
    return symbol                  # stock / index ticker as-is


def parse_yahoo_chart(payload: dict) -> list[dict]:
    """Turn a Yahoo v8 chart JSON payload into candle dicts (skips null bars)."""
    res = (payload.get("chart") or {}).get("result") or []
    if not res:
        err = (payload.get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo returned no data: {err}")
    r = res[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators") or {}).get("quote", [{}])[0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        out.append({"time": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
                    "open": float(o), "high": float(h), "low": float(l),
                    "close": float(c), "volume": int(v)})
    return out


def aggregate(candles: list[dict], factor: int) -> list[dict]:
    if factor <= 1:
        return candles
    out = []
    start = len(candles) % factor
    for i in range(start, len(candles), factor):
        grp = candles[i:i + factor]
        if len(grp) < factor:
            break
        out.append({"time": grp[0]["time"], "open": grp[0]["open"],
                    "high": max(c["high"] for c in grp), "low": min(c["low"] for c in grp),
                    "close": grp[-1]["close"], "volume": sum(c["volume"] for c in grp)})
    return out


_yahoo_down_until = 0.0   # circuit breaker: skip Yahoo briefly after a network failure


def fetch_candles_yahoo(symbol: str, timeframe: str = "H1", count: int = 200,
                        timeout: float = 15.0) -> list[dict]:
    global _yahoo_down_until
    if timeframe not in YAHOO_TF:
        raise RuntimeError(f"Unknown timeframe: {timeframe}")
    if time.time() < _yahoo_down_until:
        raise RuntimeError("Yahoo unreachable (network) — retrying shortly")
    interval, rng, factor = YAHOO_TF[timeframe]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.request.quote(yahoo_ticker(symbol))}?interval={interval}&range={rng}")
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            candles = aggregate(parse_yahoo_chart(payload), factor)
            if not candles:
                raise RuntimeError("empty candle list")
            return candles[-count:]
        except urllib.error.HTTPError as e:      # server answered: rate limit etc.
            last_err = e
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:  # no network
            last_err = e
            _yahoo_down_until = time.time() + 45
            break
        except (RuntimeError, ValueError) as e:
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"Yahoo fetch failed for {symbol} {timeframe}: {last_err}")


# ══════════════════════════════════════════════════════════════════════════════
# Mock
# ══════════════════════════════════════════════════════════════════════════════

def generate_mock_candles(n: int = 200, base_price: float = 1.08500,
                          volatility: float = 0.0015, trend_bias: float = 0.0001,
                          timeframe: str = "H1", seed: Optional[int] = None) -> list[dict]:
    rnd = random.Random(seed)
    step = TF_SECONDS.get(timeframe, 3600)
    candles, price = [], base_price
    now = int(time.time()) // step * step - n * step
    for i in range(n):
        disp = rnd.random() < 0.06
        size = volatility * (rnd.uniform(2.5, 4.0) if disp else rnd.uniform(0.3, 1.2))
        direction = 1 if rnd.random() > 0.48 else -1
        o = price
        c = price + direction * size + trend_bias
        h = max(o, c) + rnd.uniform(0, size * 0.4)
        l = min(o, c) - rnd.uniform(0, size * 0.4)
        if i > 10 and rnd.random() < 0.08:
            h = max(h, candles[-rnd.randint(2, 8)]["high"] + rnd.uniform(-0.00005, 0.00005))
        candles.append({"time": datetime.fromtimestamp(now + i * step, tz=timezone.utc).isoformat(),
                        "open": round(o, 5), "high": round(h, 5), "low": round(l, 5),
                        "close": round(c, 5), "volume": rnd.randint(500, 5000)})
        price = c
    return candles


# ══════════════════════════════════════════════════════════════════════════════
# Helpers + main entry point
# ══════════════════════════════════════════════════════════════════════════════

def pip_size(symbol: str) -> float:
    s = symbol.upper()
    if any(asset in s for asset in ("XAU", "GOLD", "JPY", "XAG", "OIL", "WTI", "ETH")):
        return 0.01
    if "BTC" in s or s in {"US30", "USTECH", "NAS100", "SPX500", "UK100"}:
        return 1.0
    return 0.0001


def fetch_candles(symbol: str = "EURUSD", timeframe: str = "H1", count: int = 200,
                  use_mock: bool = False, source: str = "auto") -> list[dict]:
    """Fetch candles. Keeps the v1 signature so api.py keeps working."""
    global _last_source
    if use_mock or source == "mock":
        _last_source = "mock"
        return generate_mock_candles(count, timeframe=timeframe)
    errors = []
    if source in ("auto", "mt5") and MT5_AVAILABLE:
        try:
            c = fetch_candles_mt5(symbol, timeframe, count)
            _last_source = "mt5"
            return c
        except Exception as e:
            errors.append(f"mt5: {e}")
            if source == "mt5":
                raise
    if source in ("auto", "yahoo"):
        try:
            c = fetch_candles_yahoo(symbol, timeframe, count)
            _last_source = "yahoo"
            return c
        except Exception as e:
            errors.append(f"yahoo: {e}")
    raise RuntimeError("No data source available — " + " | ".join(errors or ["MT5 not installed"]))


# Attach to MT5 at import (as before) but never crash if it is not running.
if MT5_AVAILABLE:
    try:
        connect_mt5()
    except Exception as _e:  # pragma: no cover
        print(f"[data] MT5 auto-connect skipped: {_e}")
