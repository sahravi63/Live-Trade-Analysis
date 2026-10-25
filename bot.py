"""
Market Analysis Bot
===================
Automates the SMC engine end to end:

  every N seconds, for each symbol x timeframe in config.json
    fetch candles (MT5 -> Yahoo -> error)        data_connector.py
    SMC + AMD signal -> exec filter -> risk      scalp_orchestrator.py
    news blackout around high-impact releases     news_filter.py
    Time Machine: Monte Carlo + history replay    timemachine.py
    if everything passes -> open paper trade      paper_broker.py
                            (or MT5 demo order)   mt5_executor.py
    alert (console / log / sound / Telegram)      alerts.py
    manage open trades (SL / TP / time exit)
  writes bot_output/latest_scan.json, journal.jsonl, trades.csv

Usage
  python bot.py run                 # loop forever (Ctrl+C to stop)
  python bot.py once                # one scan, print table, exit
  python bot.py status              # paper account + open trades
  python bot.py backtest EURUSD M5 --count 1000
  python bot.py reset-paper         # wipe paper trades, restore balance
  python bot.py test-alert          # check your alert channels

Not financial advice. Paper trading is the default; MT5 execution only works
on a demo account and must be switched on in config.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import data_connector as dc
from market_structure import analyze_market_structure
from liquidity import analyze_liquidity
from displacement import analyze_displacement, analyze_retracement
from amd_phases import analyze_amd
from decision_engine import Signal, calculate_atr
from scalp_orchestrator import ScalpOrchestrator
from risk_manager import RiskManager
from timemachine import monte_carlo, historical_replay, verdict
from news_filter import NewsFilter
from paper_broker import PaperBroker
from alerts import Alerter

OUT_DIR = os.path.join(BASE_DIR, "bot_output")
STATE_DIR = os.path.join(BASE_DIR, "bot_state")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "watchlist": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"],
    "timeframes": ["M5", "M15"],
    "scan_interval_seconds": 60,
    "candles": 300,
    "data_source": "auto",
    "use_closed_bars_only": True,
    "spread": {"assumed_pips": {"default": 1.0}, "max_pips": {"default": 1.5}},
    "account": {"balance": 10000, "risk_pct": 0.005, "max_daily_loss_pct": 0.02,
                "max_open_trades": 2, "max_consecutive_losses": 3, "cooldown_seconds": 60},
    "filters": {"min_atr_pct": 0.00025, "max_atr_pct": 0.003, "min_liquidity_score": 0.55,
                "min_rr": 1.8, "min_confidence": 0.45},
    "timemachine": {"enabled": True, "sims": 5000, "horizon_bars": 48, "history_replay": True,
                    "min_expected_r": 0.0, "min_history_samples": 5},
    "news": {"enabled": True, "window_minutes": 30, "impacts": ["High"]},
    "execution": {"mode": "paper", "max_hold_bars": 48, "reentry_block_minutes": 120},
    "alerts": {"sound": True, "telegram": True, "alert_on_blocked_setups": False},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = DEFAULT_CONFIG
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = _merge(DEFAULT_CONFIG, json.load(fh))
    cfg["execution"]["mode"] = str(cfg["execution"].get("mode", "paper")).lower()
    if cfg["execution"]["mode"] not in ("paper", "mt5-demo", "alerts-only"):
        raise ValueError("execution.mode must be 'paper', 'mt5-demo' or 'alerts-only'")
    return cfg


def _per_symbol(table: dict, symbol: str, fallback: float) -> float:
    return float(table.get(symbol.upper(), table.get("default", fallback)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


class MarketBot:
    def __init__(self, config: Optional[dict] = None, out_dir: str = OUT_DIR,
                 state_dir: str = STATE_DIR):
        self.cfg = config or load_config()
        self.out_dir, self.state_dir = out_dir, state_dir
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)

        acc, flt = self.cfg["account"], self.cfg["filters"]
        self.orch = ScalpOrchestrator(
            account_balance=acc["balance"], risk_pct=acc["risk_pct"],
            min_atr_pct=flt["min_atr_pct"], max_atr_pct=flt["max_atr_pct"],
            min_liquidity_score=flt["min_liquidity_score"],
            min_rr=flt["min_rr"], min_confidence=flt["min_confidence"])
        self.orch.risk_mgr = RiskManager(
            account_balance=acc["balance"], risk_pct_per_trade=acc["risk_pct"],
            max_daily_loss_pct=acc["max_daily_loss_pct"],
            min_cooldown_seconds=acc["cooldown_seconds"],
            max_open_trades=acc["max_open_trades"],
            max_consecutive_losses=acc["max_consecutive_losses"])

        self.broker = PaperBroker(os.path.join(state_dir, "paper.json"),
                                  starting_balance=acc["balance"],
                                  max_hold_bars=self.cfg["execution"]["max_hold_bars"])
        nw = self.cfg["news"]
        self.news = NewsFilter(os.path.join(state_dir, "news_cache.json"),
                               window_minutes=nw["window_minutes"],
                               impacts=tuple(nw["impacts"]), enabled=nw["enabled"])
        al = self.cfg["alerts"]
        self.alerter = Alerter(os.path.join(out_dir, "alerts.log"),
                               sound=al["sound"], telegram=al["telegram"])

        self.risk_path = os.path.join(state_dir, "risk.json")
        self.lock_path = os.path.join(state_dir, "bot.lock")
        self.journal_path = os.path.join(out_dir, "journal.jsonl")
        self.scan_path = os.path.join(out_dir, "latest_scan.json")
        self._recent_entries: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.cycles = 0
        self.last_scan_at: Optional[str] = None
        self.next_scan_at: Optional[str] = None
        self.last_rows: list[dict] = []
        self.last_errors: list[str] = []
        self._load_risk()

    # ── state ────────────────────────────────────────────────────────────────
    def _load_risk(self) -> None:
        rm = self.orch.risk_mgr
        if os.path.exists(self.risk_path):
            try:
                with open(self.risk_path, encoding="utf-8") as fh:
                    d = json.load(fh)
                if d.get("day") == _now().date().isoformat():
                    rm.load_dict(d["risk"])
            except (OSError, ValueError, KeyError) as e:
                print(f"[bot] risk state unreadable ({e}); starting the day fresh")
        # open positions always count against the risk manager
        rm.state.open_trades = {p.id: p.entry for p in self.broker.open_positions()}
        self._risk_day = _now().date().isoformat()
        self._save_risk()

    def _save_risk(self) -> None:
        _write_json(self.risk_path, {"day": self._risk_day, "risk": self.orch.risk_mgr.to_dict()})

    def _roll_day(self) -> None:
        today = _now().date().isoformat()
        if today != self._risk_day:
            self.orch.reset_day(new_balance=self.broker.equity())
            self.orch.risk_mgr.state.open_trades = {p.id: p.entry for p in self.broker.open_positions()}
            self._risk_day = today
            self._save_risk()
            self._journal("day_reset", {"balance": self.broker.equity()})

    def _journal(self, event: str, data: dict) -> None:
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now().isoformat(), "event": event, **data}, default=str) + "\n")

    def _append_trade_csv(self, p) -> None:
        path = os.path.join(self.out_dir, "trades.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["id", "mode", "symbol", "tf", "direction", "setup", "entry", "sl", "tp",
                            "lots", "opened_at", "closed_at", "exit", "reason", "pnl", "r"])
            w.writerow([p.id, p.mode, p.symbol, p.timeframe, p.direction, p.setup, p.entry, p.sl,
                        p.tp, p.lots, p.opened_at, p.closed_at, p.exit_price, p.exit_reason,
                        p.pnl, p.r_multiple])

    # ── lock so the CLI bot and the API bot never run at the same time ─────
    def _acquire_lock(self) -> None:
        stale_after = max(180, 3 * self.cfg["scan_interval_seconds"])
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, encoding="utf-8") as fh:
                    d = json.load(fh)
                if d.get("pid") != os.getpid() and time.time() - d.get("heartbeat", 0) < stale_after:
                    raise RuntimeError(
                        f"another bot (pid {d.get('pid')}) is already running — stop it first")
            except (OSError, ValueError):
                pass
        self._heartbeat()

    def _heartbeat(self) -> None:
        _write_json(self.lock_path, {"pid": os.getpid(), "heartbeat": time.time()})

    def _release_lock(self) -> None:
        try:
            with open(self.lock_path, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("pid") == os.getpid():
                _write_json(self.lock_path, {"pid": None, "heartbeat": 0})
        except (OSError, ValueError):
            pass

    # ── one symbol x timeframe ──────────────────────────────────────────────
    def analyze(self, symbol: str, tf: str) -> dict:
        cfg = self.cfg
        candles = dc.fetch_candles(symbol, tf, cfg["candles"], source=cfg["data_source"])
        source = dc.last_source()
        live_price = candles[-1]["close"]
        bars = candles[:-1] if cfg["use_closed_bars_only"] and len(candles) > 60 else candles

        # manage open trades on this symbol before looking for new ones
        self._manage_positions(symbol, tf, candles)

        spread = dc.get_spread_pips(symbol) if source == "mt5" else None
        spread_src = "mt5" if spread is not None else "assumed"
        if spread is None:
            spread = _per_symbol(cfg["spread"]["assumed_pips"], symbol, 1.0)
        self.orch.max_spread_pips = _per_symbol(cfg["spread"]["max_pips"], symbol, 1.5)

        st = analyze_market_structure(bars)
        lq = analyze_liquidity(bars, live_price)
        dp = analyze_displacement(bars)
        rt = analyze_retracement(bars, dp.fvgs, live_price, dp)
        amd = analyze_amd(bars, live_price)
        d = self.orch.evaluate(candles=bars, structure=st, liquidity=lq, displacement=dp,
                               retracement=rt, current_price=live_price, symbol=symbol,
                               timeframe=tf, spread_pips=spread, amd=amd)
        sig = d.signal
        blocks = list(d.block_reasons)
        ready = d.ready_to_trade
        row = {
            "symbol": symbol, "timeframe": tf, "source": source, "time": _now().isoformat(),
            "bar_time": bars[-1]["time"], "price": live_price,
            "spread_pips": spread, "spread_source": spread_src,
            "trend": st.trend.value, "amd_phase": amd.phase.value, "amd_bias": amd.bias,
            "signal": sig.signal.value, "setup": sig.setup_type,
            "confidence": round(sig.confidence, 3), "rr": round(sig.rr_ratio, 2),
            "entry": d.exec_entry, "sl": d.exec_sl, "tp": d.exec_tp,
            "lots": d.sizing.lots, "risk_amount": d.sizing.risk_amount,
            "session": d.scalp_filter.session, "reasons": sig.reasons[:6],
            "timemachine": None, "news": None, "action": "none",
        }

        if sig.signal != Signal.WAIT:
            # Execution sanity at the real market price (the engine sizes from
            # the middle of its entry zone, the bot fills at market).
            atr = calculate_atr(bars)
            zl, zh = sig.entry_zone
            tol = max(atr * 0.3, (zh - zl) * 0.25)
            e, s_, t_ = d.exec_entry, d.exec_sl, d.exec_tp
            if e is not None and not (zl - tol <= e <= zh + tol):
                ready = False
                blocks.append(f"Waiting for price to reach entry zone {zl:.5f}-{zh:.5f}")
            if e and s_ and t_:
                good_side = (s_ < e < t_) if sig.signal == Signal.BUY else (t_ < e < s_)
                exec_rr = abs(t_ - e) / abs(e - s_) if abs(e - s_) > 0 else 0.0
                row["exec_rr"] = round(exec_rr, 2)
                if not good_side:
                    ready = False
                    blocks.append("Levels invalid at market price")
                elif exec_rr < cfg["filters"]["min_rr"]:
                    ready = False
                    blocks.append(f"R:R at market {exec_rr:.2f} < {cfg['filters']['min_rr']}")

            news = self.news.check(symbol)
            row["news"] = news
            if news["blocked"]:
                ready = False
                blocks.append("News blackout: " + ", ".join(
                    f"{e['currency']} {e['title']}" for e in news["events"]))
            if self.broker.open_positions(symbol):
                ready = False
                blocks.append(f"Already in a trade on {symbol}")
            key = f"{symbol}|{tf}|{sig.signal.value}|{round(sig.stop_loss, 3)}"
            block_min = cfg["execution"]["reentry_block_minutes"]
            if time.time() - self._recent_entries.get(key, 0) < block_min * 60:
                ready = False
                blocks.append("Same setup already traded recently")

            tmc = cfg["timemachine"]
            if tmc["enabled"] and d.exec_entry and d.exec_sl and d.exec_tp:
                odds = monte_carlo(candles, sig.signal.value, d.exec_entry, d.exec_sl, d.exec_tp,
                                   horizon=tmc["horizon_bars"], sims=tmc["sims"], seed=19)
                replay = None
                if ready and tmc["history_replay"]:
                    replay = historical_replay(candles, symbol, sig.signal.value,
                                               horizon=tmc["horizon_bars"],
                                               min_rr=cfg["filters"]["min_rr"],
                                               min_confidence=cfg["filters"]["min_confidence"])
                row["timemachine"] = {"monte_carlo": odds.to_dict(),
                                      "history": replay.to_dict() if replay else None,
                                      "verdict": verdict(odds, replay, tmc["min_history_samples"])}
                if ready and odds.expected_r < tmc["min_expected_r"]:
                    ready = False
                    blocks.append(f"TimeMachine: Monte Carlo edge {odds.expected_r:+.2f}R")
                if (ready and replay and replay.fired >= tmc["min_history_samples"]
                        and replay.expected_r < tmc["min_expected_r"]):
                    ready = False
                    blocks.append(f"TimeMachine: history edge {replay.expected_r:+.2f}R "
                                  f"over {replay.fired} past signals")

            if ready:
                row["action"] = self._execute(row, d, sig, bars[-1]["time"], key)
            elif cfg["alerts"]["alert_on_blocked_setups"]:
                self.alerter.send(f"{symbol} {tf} {sig.signal.value} setup blocked",
                                  "; ".join(blocks[:3]), "info",
                                  key=f"blocked|{symbol}|{tf}|{sig.signal.value}")

        row["ready"] = ready
        row["block_reasons"] = blocks[:6]
        return row

    def _execute(self, row: dict, d, sig, bar_time: str, key: str) -> str:
        mode = self.cfg["execution"]["mode"]
        sym, tf, dirn = row["symbol"], row["timeframe"], sig.signal.value
        tm = row.get("timemachine") or {}
        body = (f"Entry {d.exec_entry} | SL {d.exec_sl} | TP {d.exec_tp} | Lots {d.sizing.lots} "
                f"| Risk ${d.sizing.risk_amount} | R:R {sig.rr_ratio:.1f} | Conf {sig.confidence:.0%}"
                f"\n    {tm.get('verdict', '')}")
        if mode == "alerts-only":
            self.alerter.send(f"SIGNAL {sym} {tf} {dirn} [{sig.setup_type}]", body, "trade",
                              key=key)
            self._recent_entries[key] = time.time()
            self._journal("signal", row)
            return "alert sent (alerts-only mode)"

        ticket, fill_mode, entry = None, "paper", d.exec_entry
        if mode == "mt5-demo":
            from mt5_executor import place_market_order, ExecutionRefused
            try:
                res = place_market_order(sym, dirn, d.sizing.lots, d.exec_sl, d.exec_tp,
                                         comment=f"smc {sig.setup_type}")
                ticket, fill_mode, entry = res["ticket"], "mt5-demo", res["price"]
            except (ExecutionRefused, RuntimeError) as e:
                self.alerter.send(f"MT5 order NOT placed for {sym}", str(e), "error")
                self._journal("order_failed", {"symbol": sym, "error": str(e)})
                return f"mt5 order failed: {e}"

        p = self.broker.open(sym, tf, dirn, entry, d.exec_sl, d.exec_tp, d.sizing.lots,
                             d.sizing.risk_amount, bar_time, setup=sig.setup_type,
                             confidence=sig.confidence, timemachine=tm,
                             mode=fill_mode, ticket=ticket)
        self.orch.confirm_entry(p.id, entry)
        self._recent_entries[key] = time.time()
        self._save_risk()
        self._journal("open", {"position": p.__dict__})
        label = "MT5 DEMO" if fill_mode == "mt5-demo" else "PAPER"
        self.alerter.send(f"{label} {dirn} {sym} {tf} [{sig.setup_type}] #{p.id}", body, "trade")
        return f"opened {label.lower()} #{p.id}"

    def _manage_positions(self, symbol: str, tf: str, candles: list[dict]) -> None:
        closed = self.broker.update(symbol, tf, candles)
        # MT5 demo positions: ask the broker whether they have closed
        for p in self.broker.open_positions(symbol):
            if p.mode == "mt5-demo" and p.ticket:
                from mt5_executor import position_closed_pnl
                res = position_closed_pnl(p.ticket)
                if res:
                    exit_price, pnl = res
                    self.broker.close(p, exit_price, "closed on MT5")
                    p.pnl = pnl
                    self.broker.save()
                    closed.append(p)
        for p in closed:
            self.orch.close_trade(p.id, p.pnl)
            self._save_risk()
            self._append_trade_csv(p)
            self._journal("close", {"position": p.__dict__})
            self.alerter.send(f"CLOSED {p.symbol} {p.direction} #{p.id} — {p.exit_reason}",
                              f"P&L ${p.pnl:+.2f} ({p.r_multiple:+.2f}R) | equity ${self.broker.equity()}",
                              "close")

    # ── full scan ───────────────────────────────────────────────────────────
    def scan_once(self) -> list[dict]:
        self._roll_day()
        rows, errors = [], []
        for sym in self.cfg["watchlist"]:
            for tf in self.cfg["timeframes"]:
                try:
                    rows.append(self.analyze(sym, tf))
                except Exception as e:
                    msg = f"{sym} {tf}: {e}"
                    errors.append(msg)
                    rows.append({"symbol": sym, "timeframe": tf, "signal": "ERROR",
                                 "ready": False, "block_reasons": [str(e)], "action": "none",
                                 "time": _now().isoformat()})
                    if os.getenv("BOT_DEBUG"):
                        traceback.print_exc()
        self.cycles += 1
        self.last_scan_at = _now().isoformat()
        self.last_rows, self.last_errors = rows, errors
        if errors and len(errors) == len(rows):
            self.alerter.send("All data fetches failed", errors[0], "warn", key="all-failed")
        self._save_risk()
        _write_json(self.scan_path, {"scanned_at": self.last_scan_at, "cycle": self.cycles,
                                     "mode": self.cfg["execution"]["mode"],
                                     "rows": rows, "errors": errors})
        return rows

    def run_forever(self) -> None:
        self._acquire_lock()
        self.running = True
        self._stop.clear()
        interval = self.cfg["scan_interval_seconds"]
        self.alerter.send("Bot started",
                          f"mode={self.cfg['execution']['mode']} | {len(self.cfg['watchlist'])} symbols "
                          f"x {self.cfg['timeframes']} | every {interval}s | alerts: "
                          f"{', '.join(self.alerter.channels)}")
        try:
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    rows = self.scan_once()
                    print_table(rows, self)
                except Exception as e:
                    self.alerter.send("Scan crashed", f"{e}", "error", key="scan-crash")
                    traceback.print_exc()
                self._heartbeat()
                wait = max(5.0, interval - (time.time() - t0))
                self.next_scan_at = datetime.fromtimestamp(time.time() + wait, tz=timezone.utc).isoformat()
                self._stop.wait(wait)
        finally:
            self.running = False
            self.next_scan_at = None
            self._release_lock()
            self.alerter.send("Bot stopped")

    # ── background thread control (used by the API) ─────────────────────────
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._acquire_lock()  # raises if another bot is running
        self._thread = threading.Thread(target=self.run_forever, name="market-bot", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        if not (self._thread and self._thread.is_alive()):
            return False
        self._stop.set()
        return True

    def external_running(self) -> bool:
        """True when a bot in another process (e.g. `python bot.py run`) is alive."""
        try:
            with open(self.lock_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return False
        stale_after = max(180, 3 * self.cfg["scan_interval_seconds"])
        return bool(d.get("pid")) and d.get("pid") != os.getpid() and \
            time.time() - d.get("heartbeat", 0) < stale_after

    def refresh_from_disk(self) -> None:
        """Reload paper trades + last scan written by a bot in another process."""
        self.broker._load()
        if os.path.exists(self.scan_path):
            try:
                with open(self.scan_path, encoding="utf-8") as fh:
                    d = json.load(fh)
                self.last_rows = d.get("rows", [])
                self.last_scan_at = d.get("scanned_at")
                self.last_errors = d.get("errors", [])
            except (OSError, ValueError):
                pass

    def status(self) -> dict:
        if not self.running:
            self.refresh_from_disk()
        closed = self.broker.closed_positions()
        return {
            "running": self.running or self.external_running(),
            "running_in": "api" if self.running else ("cli" if self.external_running() else None),
            "mode": self.cfg["execution"]["mode"],
            "cycles": self.cycles,
            "last_scan_at": self.last_scan_at,
            "next_scan_at": self.next_scan_at,
            "watchlist": self.cfg["watchlist"],
            "timeframes": self.cfg["timeframes"],
            "interval_seconds": self.cfg["scan_interval_seconds"],
            "data_source": dc.last_source(),
            "mt5_connected": dc.mt5_connected(),
            "alert_channels": self.alerter.channels,
            "risk": self.orch.risk_status(),
            "paper": self.broker.stats(),
            "open_positions": [p.__dict__ for p in self.broker.open_positions()],
            "recent_closed": [p.__dict__ for p in closed[-15:]][::-1],
            "upcoming_news": self.news.upcoming(self.cfg["watchlist"], 24)[:10],
            "news_note": self.news.error,
            "errors": self.last_errors,
            "rows": self.last_rows,
        }


# ── console output ───────────────────────────────────────────────────────────
def print_table(rows: list[dict], bot: MarketBot) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n── scan {bot.cycles} @ {ts} ── mode={bot.cfg['execution']['mode']} "
          f"equity=${bot.broker.equity()} open={len(bot.broker.open_positions())}")
    print(f"{'SYMBOL':8} {'TF':4} {'SRC':6} {'PRICE':>11} {'TREND':9} {'AMD':13} {'SIGNAL':6} "
          f"{'CONF':>5} {'RR':>4}  STATUS")
    for r in rows:
        if r.get("signal") == "ERROR":
            print(f"{r['symbol']:8} {r['timeframe']:4} error: {r['block_reasons'][0][:80]}")
            continue
        status = r["action"] if r["action"] != "none" else (
            "READY" if r["ready"] else (r["block_reasons"][0][:60] if r["block_reasons"] else "-"))
        print(f"{r['symbol']:8} {r['timeframe']:4} {r['source']:6} {r['price']:>11.5f} "
              f"{r['trend'][:9]:9} {r['amd_phase'][:13]:13} {r['signal']:6} "
              f"{r['confidence']:>5.0%} {r['rr']:>4.1f}  {status}")


def cmd_backtest(symbol: str, tf: str, count: int, cfg: dict) -> None:
    candles = dc.fetch_candles(symbol, tf, count, source=cfg["data_source"])
    print(f"{symbol} {tf}: {len(candles)} bars from {dc.last_source()} "
          f"({candles[0]['time']} → {candles[-1]['time']})")
    t0 = time.time()
    rep = historical_replay(candles, symbol, None, horizon=cfg["timemachine"]["horizon_bars"],
                            min_rr=cfg["filters"]["min_rr"],
                            min_confidence=cfg["filters"]["min_confidence"],
                            max_samples=max(100, count - 200))
    print(f"Signals fired: {rep.fired} | wins {rep.wins} | losses {rep.losses} | open {rep.open}")
    print(f"Win rate (decided): {rep.win_rate:.1%} | expectancy: {rep.expected_r:+.2f}R per signal "
          f"| {time.time() - t0:.1f}s")
    for s in rep.samples[-15:]:
        print(f"  {s['time']}  {s['signal']:4} {s['setup'][:18]:18} {s['outcome']:5} {s['r']:+.2f}R")
    print("Signal-engine replay only (no spread/session/risk filters). Not a guarantee.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="SMC Market Analysis Bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("status")
    sub.add_parser("reset-paper")
    sub.add_parser("test-alert")
    bt = sub.add_parser("backtest")
    bt.add_argument("symbol")
    bt.add_argument("timeframe")
    bt.add_argument("--count", type=int, default=1000)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--mock", action="store_true", help="use simulated candles")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.mock:
        cfg["data_source"] = "mock"
        cfg["news"]["enabled"] = False
    if args.cmd == "backtest":
        return cmd_backtest(args.symbol.upper(), args.timeframe.upper(), args.count, cfg)

    bot = MarketBot(cfg)
    if args.cmd == "run":
        try:
            bot.run_forever()
        except KeyboardInterrupt:
            print("\nstopping…")
    elif args.cmd == "once":
        rows = bot.scan_once()
        print_table(rows, bot)
        for r in rows:
            if r.get("timemachine"):
                print(f"  {r['symbol']} {r['timeframe']} TimeMachine: {r['timemachine']['verdict']}")
        print(f"\nWrote {bot.scan_path}")
    elif args.cmd == "status":
        print(json.dumps(bot.status(), indent=1, default=str))
    elif args.cmd == "reset-paper":
        bot.broker.reset(cfg["account"]["balance"])
        bot.orch.reset_day(cfg["account"]["balance"])
        bot._save_risk()
        print(f"Paper account reset to ${cfg['account']['balance']}")
    elif args.cmd == "test-alert":
        bot.alerter.send("Test alert from the Market Analysis Bot",
                         f"channels: {', '.join(bot.alerter.channels)}", "trade")


if __name__ == "__main__":
    main()
