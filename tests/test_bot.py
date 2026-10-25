"""Offline tests for the Market Analysis Bot.   python -m unittest discover -s tests -v"""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_connector as dc
from risk_manager import RiskManager
from paper_broker import PaperBroker
from timemachine import monte_carlo, forward_outcome
from news_filter import NewsFilter, parse_events
from market_structure import analyze_market_structure
from liquidity import analyze_liquidity
from displacement import analyze_displacement, analyze_retracement
from amd_phases import analyze_amd
from decision_engine import decide, Signal
import bot as botmod


class TestData(unittest.TestCase):
    def test_yahoo_parse_skips_nulls(self):
        payload = {"chart": {"result": [{"timestamp": [1700000000, 1700000300, 1700000600],
                   "indicators": {"quote": [{"open": [1.1, None, 1.2], "high": [1.2, 1.3, 1.25],
                                             "low": [1.0, 1.1, 1.15], "close": [1.15, 1.2, 1.22],
                                             "volume": [0, 0, 0]}]}}], "error": None}}
        c = dc.parse_yahoo_chart(payload)
        self.assertEqual(len(c), 2)
        self.assertEqual(c[1]["close"], 1.22)

    def test_aggregate_h4(self):
        c = dc.generate_mock_candles(10, timeframe="H1", seed=1)
        h4 = dc.aggregate(c, 4)
        self.assertEqual(len(h4), 2)
        self.assertEqual(h4[0]["high"], max(x["high"] for x in c[2:6]))

    def test_yahoo_tickers(self):
        self.assertEqual(dc.yahoo_ticker("EURUSD"), "EURUSD=X")
        self.assertEqual(dc.yahoo_ticker("XAUUSD"), "GC=F")

    def test_mock_needs_no_credentials(self):
        self.assertEqual(len(dc.fetch_candles("EURUSD", "M5", 120, use_mock=True)), 120)


class TestRisk(unittest.TestCase):
    def test_sizing_fx_jpy_gold(self):
        rm = RiskManager(account_balance=10_000)
        for sym, e, s in [("EURUSD", 1.0850, 1.0840), ("USDJPY", 150.0, 149.9), ("XAUUSD", 2400, 2397)]:
            r = rm.size_trade(e, s, sym, "BUY")
            self.assertTrue(r.allowed, f"{sym}: {r.reason}")
            self.assertLess(r.lots * (r.sl_pips + 1) * {"EURUSD": 10, "USDJPY": 9.3, "XAUUSD": 1}[sym], 55)

    def test_roundtrip_state(self):
        rm = RiskManager()
        rm.record_trade_open("T1", 1.1)
        rm.record_trade_close("T1", -20)
        rm2 = RiskManager()
        rm2.load_dict(rm.to_dict())
        self.assertEqual(rm2.status()["daily_pnl"], -20)


class TestPaper(unittest.TestCase):
    def setUp(self):
        self.b = PaperBroker(os.path.join(tempfile.mkdtemp(), "p.json"), max_hold_bars=2)
        self.t0 = "2026-10-24T10:00:00+00:00"

    def bar(self, minute, h, l, c=1.1):
        t = (datetime(2026, 10, 24, 10, minute, tzinfo=timezone.utc)).isoformat()
        return {"time": t, "open": 1.1, "high": h, "low": l, "close": c, "volume": 0}

    def test_tp_and_sl_and_same_bar(self):
        p = self.b.open("EURUSD", "M5", "BUY", 1.1, 1.099, 1.102, 0.5, 50, self.t0)
        closed = self.b.update("EURUSD", "M5", [self.bar(5, 1.1025, 1.0995)])
        self.assertEqual(closed[0].exit_reason, "take profit")
        self.assertAlmostEqual(closed[0].pnl, 100.0)
        self.b.open("EURUSD", "M5", "BUY", 1.1, 1.099, 1.102, 0.5, 50, self.t0)
        closed = self.b.update("EURUSD", "M5", [self.bar(5, 1.103, 1.098)])
        self.assertEqual(closed[0].exit_reason, "stop loss")  # both touched -> SL

    def test_time_exit(self):
        self.b.open("EURUSD", "M5", "SELL", 1.1, 1.101, 1.098, 0.5, 50, self.t0)
        closed = self.b.update("EURUSD", "M5", [self.bar(5, 1.1005, 1.0995, 1.0995),
                                                  self.bar(10, 1.1005, 1.0995, 1.0995)])
        self.assertTrue(closed[0].exit_reason.startswith("time exit"))
        self.assertGreater(closed[0].pnl, 0)


class TestEngine(unittest.TestCase):
    def test_levels_always_on_correct_side(self):
        for seed in range(150):
            c = dc.generate_mock_candles(250, seed=seed, timeframe="M5", volatility=0.0005)
            p = c[-1]["close"]
            st, dp = analyze_market_structure(c), analyze_displacement(c)
            s = decide(c, st, analyze_liquidity(c, p), dp, analyze_retracement(c, dp.fvgs, p, dp),
                       p, amd=analyze_amd(c, p))
            if s.signal == Signal.BUY:
                self.assertLess(s.stop_loss, s.entry_zone[0])
                self.assertGreater(s.take_profit, s.entry_zone[1])
            elif s.signal == Signal.SELL:
                self.assertGreater(s.stop_loss, s.entry_zone[1])
                self.assertLess(s.take_profit, s.entry_zone[0])


class TestTimeMachine(unittest.TestCase):
    def test_deterministic_and_sums_to_one(self):
        c = dc.generate_mock_candles(300, seed=3, timeframe="M5", volatility=0.0004)
        p = c[-1]["close"]
        a = monte_carlo(c, "BUY", p, p - 0.001, p + 0.002, sims=2000, seed=19)
        b = monte_carlo(c, "BUY", p, p - 0.001, p + 0.002, sims=2000, seed=19)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a.tp_first + a.sl_first + a.still_open, 1.0, places=3)

    def test_forward_outcome(self):
        c = [{"high": 1.0, "low": 1.0, "close": 1.0},
             {"high": 1.03, "low": 0.995, "close": 1.02}]
        self.assertEqual(forward_outcome(c, 0, "BUY", 1.0, 0.99, 1.02, 5), ("win", 2.0))


class TestNews(unittest.TestCase):
    def test_blackout(self):
        nf = NewsFilter(os.path.join(tempfile.mkdtemp(), "n.json"), window_minutes=30)
        now = datetime.now(timezone.utc)
        nf.events = parse_events([{"title": "NFP", "country": "USD", "impact": "High",
                                   "date": (now + timedelta(minutes=5)).isoformat()}])
        nf.loaded_at = 1e12
        self.assertTrue(nf.check("XAUUSD")["blocked"])
        self.assertFalse(nf.check("EURGBP")["blocked"])


class TestBotWalkForward(unittest.TestCase):
    """Drive the whole bot through a moving mock market, bar by bar."""

    def test_walk_forward(self):
        tmp = tempfile.mkdtemp()
        cfg = copy.deepcopy(botmod.DEFAULT_CONFIG)
        cfg.update({"watchlist": ["EURUSD"], "timeframes": ["M5"], "candles": 250,
                    "data_source": "mock"})
        cfg["news"]["enabled"] = False
        cfg["alerts"].update({"sound": False, "telegram": False})
        cfg["account"]["cooldown_seconds"] = 0
        cfg["filters"]["min_liquidity_score"] = 0.0     # ignore wall-clock session
        cfg["timemachine"].update({"sims": 500, "history_replay": False, "min_expected_r": -99})
        cfg["execution"]["reentry_block_minutes"] = 0
        series = dc.generate_mock_candles(900, seed=12, timeframe="M5", volatility=0.0005)
        state = {"i": 300}
        orig = dc.fetch_candles
        dc.fetch_candles = lambda s, tf, n, use_mock=False, source="auto": series[max(0, state["i"] - n):state["i"]]
        try:
            b = botmod.MarketBot(cfg, out_dir=os.path.join(tmp, "out"), state_dir=os.path.join(tmp, "st"))
            for i in range(300, 900, 2):
                state["i"] = i
                rows = b.scan_once()
                self.assertLessEqual(len(b.broker.open_positions()), cfg["account"]["max_open_trades"])
                for r in rows:
                    self.assertNotEqual(r["signal"], "ERROR", r)
        finally:
            dc.fetch_candles = orig
        for p in b.broker.positions:
            if p.direction == "BUY":
                self.assertTrue(p.sl < p.entry < p.tp)
            else:
                self.assertTrue(p.tp < p.entry < p.sl)
        with open(os.path.join(tmp, "out", "latest_scan.json")) as fh:
            self.assertIn("rows", json.load(fh))
        self.assertGreaterEqual(len(b.broker.positions), 1, "bot never opened a paper trade")
        print(f"\n    walk-forward: {b.broker.stats()}")


if __name__ == "__main__":
    unittest.main()
