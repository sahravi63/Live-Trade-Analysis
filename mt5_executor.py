"""
MT5 order executor — DEMO ACCOUNTS ONLY.

Used only when config "execution": {"mode": "mt5-demo"}. Every call checks
that the logged-in MT5 account is a demo account and refuses otherwise.
Market orders carry the bot's SL and TP on the broker side.
"""
from __future__ import annotations

from typing import Optional

from data_connector import MT5_AVAILABLE, ensure_connected, mt5

MAGIC = 260425


class ExecutionRefused(RuntimeError):
    pass


def _require_demo() -> None:
    if not MT5_AVAILABLE:
        raise ExecutionRefused("MetaTrader5 package not installed")
    if not ensure_connected():
        raise ExecutionRefused("MT5 terminal not connected")
    acct = mt5.account_info()
    if acct is None:
        raise ExecutionRefused("no MT5 account info")
    if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise ExecutionRefused(
            f"account {acct.login} is not a demo account — the bot only executes on demo")


def _round_volume(symbol: str, lots: float) -> float:
    info = mt5.symbol_info(symbol)
    if info is None:
        return round(lots, 2)
    step = info.volume_step or 0.01
    vol = max(info.volume_min, min(info.volume_max, round(lots / step) * step))
    return round(vol, 2)


def place_market_order(symbol: str, direction: str, lots: float, sl: float, tp: float,
                       comment: str = "smc-bot") -> dict:
    _require_demo()
    if not mt5.symbol_select(symbol, True):
        raise ExecutionRefused(f"symbol {symbol} unavailable")
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    is_buy = direction.upper() == "BUY"
    price = tick.ask if is_buy else tick.bid
    digits = info.digits if info else 5
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": _round_volume(symbol, lots),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": round(sl, digits),
        "tp": round(tp, digits),
        "deviation": 10,
        "magic": MAGIC,
        "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError(f"order_send returned None: {mt5.last_error()}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"order rejected: retcode={result.retcode} {result.comment}")
    return {"ticket": result.order, "price": result.price, "volume": result.volume}


def position_closed_pnl(ticket: int) -> Optional[tuple[float, float]]:
    """If the MT5 position with this ticket is closed, return (exit_price, pnl)."""
    if not (MT5_AVAILABLE and ensure_connected()):
        return None
    if mt5.positions_get(ticket=ticket):
        return None  # still open
    deals = mt5.history_deals_get(position=ticket) or []
    if not deals:
        return None
    pnl = sum(d.profit + d.commission + d.swap for d in deals)
    exit_price = deals[-1].price
    return exit_price, round(pnl, 2)
