"""Offline tests for the Market Analysis Bot.   python -m unittest discover -s tests -v"""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

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
        for symbol, ticker in {
            "BTCUSD": "BTC-USD",
            "USOIL": "CL=F",
            "USTECH": "^NDX",
            "ETHUSD": "ETH-USD",
            "XAGUSD": "SI=F",
            "US30": "^DJI",
            "SPX500": "^GSPC",
            "UK100": "^FTSE",
        }.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(dc.yahoo_ticker(symbol), ticker)

    def test_non_fx_pip_sizes(self):
        for symbol, expected in {
            "USOIL": 0.01,
            "USTECH": 1.0,
            "BTCUSD": 1.0,
        }.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(dc.pip_size(symbol), expected)

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

    def test_equity_tracks_open_position_mark(self):
        p = self.b.open("EURUSD", "M5", "BUY", 1.1, 1.099, 1.102, 0.5, 50, self.t0)
        self.b.update("EURUSD", "M5", [self.bar(5, 1.1015, 1.1005, 1.101)])
        self.assertEqual(p.mark_price, 1.101)
        self.assertEqual(p.unrealized_pnl, 50.0)
        self.assertEqual(self.b.balance(), 10_000)
        self.assertEqual(self.b.equity(), 10_050)

    def test_deposit_withdrawal_and_account_persist(self):
        self.b.deposit(500)
        self.b.withdraw(150)
        self.assertEqual(self.b.balance(), 10_350)
        with self.assertRaises(ValueError):
            self.b.withdraw(10_350.01)

        restored = PaperBroker(self.b.path)
        self.assertEqual(restored.balance(), 10_350)
        self.assertEqual([event["type"] for event in restored.funding_events],
                         ["deposit", "withdrawal"])

    def test_trailing_level_only_applies_after_update_bar(self):
        p = self.b.open("EURUSD", "M5", "BUY", 1.1, 1.099, 1.102, 0.5, 50, self.t0)
        self.b.update("EURUSD", "M5", [self.bar(5, 1.1015, 1.1009, 1.101)])
        self.b.update_position(
            p, 1.1008, p.tp, p.lots, self.bar(5, 1.1015, 1.1009)["time"],
            max_risk_amount=50,
        )
        self.assertEqual(p.sl, 1.1008)
        self.assertEqual(self.b.update("EURUSD", "M5", [
            self.bar(5, 1.1015, 1.1009, 1.101),
        ]), [])

        closed = self.b.update("EURUSD", "M5", [
            self.bar(5, 1.1015, 1.1009, 1.101),
            self.bar(10, 1.1015, 1.1007, 1.101),
        ])
        self.assertEqual(closed[0].exit_reason, "stop loss")
        self.assertEqual(closed[0].exit_price, 1.1008)

    def test_sell_stop_can_trail_down_without_widening_risk(self):
        p = self.b.open("EURUSD", "M5", "SELL", 1.1, 1.101, 1.098, 0.45, 50, self.t0)
        self.b.update("EURUSD", "M5", [self.bar(5, 1.0995, 1.0985, 1.099)])
        updated = self.b.update_position(
            p, 1.0998, p.tp, p.lots, self.bar(5, 1.0995, 1.0985, 1.099)["time"],
            max_risk_amount=50,
        )
        self.assertEqual(updated.sl, 1.0998)
        with self.assertRaisesRegex(ValueError, "cannot widen risk"):
            self.b.update_position(
                p, 1.1002, p.tp, p.lots, self.bar(5, 1.0995, 1.0985, 1.099)["time"],
                max_risk_amount=50,
            )

    def test_updated_take_profit_auto_closes_and_lot_update_obeys_risk(self):
        p = self.b.open("EURUSD", "M5", "BUY", 1.1, 1.099, 1.103, 0.45, 50, self.t0)
        self.b.update("EURUSD", "M5", [self.bar(5, 1.1014, 1.1006, 1.101)])
        updated = self.b.update_position(
            p, 1.1005, 1.1015, 0.4, self.bar(5, 1.1014, 1.1006)["time"],
            max_risk_amount=50,
        )
        self.assertEqual(updated.lots, 0.4)
        with self.assertRaisesRegex(ValueError, "above the"):
            self.b.update_position(
                p, 1.1005, 1.1015, 2.0, self.bar(5, 1.1014, 1.1006)["time"],
                max_risk_amount=50,
            )
        closed = self.b.update("EURUSD", "M5", [
            self.bar(5, 1.1014, 1.1006, 1.101),
            self.bar(10, 1.1017, 1.101, 1.1016),
        ])
        self.assertEqual(closed[0].exit_reason, "take profit")
        self.assertEqual(closed[0].exit_price, 1.1015)
        self.assertEqual(closed[0].lots, 0.4)


class TestManualPaperTrade(unittest.TestCase):
    def test_dead_bot_lock_is_not_reported_as_running(self):
        tmp = tempfile.mkdtemp()
        cfg = copy.deepcopy(botmod.DEFAULT_CONFIG)
        b = botmod.MarketBot(
            cfg, out_dir=os.path.join(tmp, "out"), state_dir=os.path.join(tmp, "st"),
        )
        with open(b.lock_path, "w", encoding="utf-8") as lock_file:
            json.dump({"pid": 2**30, "heartbeat": datetime.now(timezone.utc).timestamp()}, lock_file)

        self.assertFalse(b.external_running())
        b._acquire_lock()
        b._release_lock()

    def test_opens_with_risk_checks_and_blocks_duplicate_symbol(self):
        tmp = tempfile.mkdtemp()
        cfg = copy.deepcopy(botmod.DEFAULT_CONFIG)
        cfg["alerts"].update({"sound": False, "telegram": False})
        cfg["account"]["cooldown_seconds"] = 0
        b = botmod.MarketBot(
            cfg, out_dir=os.path.join(tmp, "out"), state_dir=os.path.join(tmp, "st"),
        )
        bar_time = "2026-10-24T10:00:00+00:00"

        p = b.open_manual_paper_trade(
            "EURUSD", "M5", "BUY", 1.1, 1.099, 1.102, bar_time, confidence=0.7,
        )
        self.assertEqual(p.mode, "paper")
        self.assertEqual(p.direction, "BUY")
        self.assertLess(p.sl, p.entry)
        self.assertGreater(p.tp, p.entry)
        self.assertIn(p.id, b.orch.risk_mgr.state.open_trades)
        with self.assertRaises(RuntimeError):
            b.open_manual_paper_trade(
                "EURUSD", "M5", "SELL", 1.1, 1.101, 1.098, bar_time,
            )

    def test_rejects_invalid_levels_and_insufficient_reward(self):
        tmp = tempfile.mkdtemp()
        cfg = copy.deepcopy(botmod.DEFAULT_CONFIG)
        cfg["alerts"].update({"sound": False, "telegram": False})
        cfg["account"]["cooldown_seconds"] = 0
        b = botmod.MarketBot(
            cfg, out_dir=os.path.join(tmp, "out"), state_dir=os.path.join(tmp, "st"),
        )
        bar_time = "2026-10-24T10:00:00+00:00"
        with self.assertRaises(ValueError):
            b.open_manual_paper_trade(
                "EURUSD", "M5", "BUY", 1.1, 1.101, 1.102, bar_time,
            )
        with self.assertRaises(ValueError):
            b.open_manual_paper_trade(
                "EURUSD", "M5", "BUY", 1.1, 1.099, 1.1005, bar_time,
            )

    def test_manual_close_updates_paper_balance_and_risk_state(self):
        tmp = tempfile.mkdtemp()
        cfg = copy.deepcopy(botmod.DEFAULT_CONFIG)
        cfg["alerts"].update({"sound": False, "telegram": False})
        cfg["account"]["cooldown_seconds"] = 0
        b = botmod.MarketBot(
            cfg, out_dir=os.path.join(tmp, "out"), state_dir=os.path.join(tmp, "st"),
        )
        position = b.open_manual_paper_trade(
            "EURUSD", "M5", "BUY", 1.1, 1.099, 1.102,
            "2026-10-24T10:00:00+00:00",
        )
        position.mark_price = 1.101
        closed = b.close_paper_position(position.id)

        self.assertEqual(closed.status, "closed")
        self.assertEqual(closed.exit_reason, "manual close")
        self.assertAlmostEqual(closed.pnl, 45.0)
        self.assertEqual(b.broker.balance(), 10_045.0)
        self.assertNotIn(position.id, b.orch.risk_mgr.state.open_trades)

    def test_unconfigured_manual_timeframe_still_auto_closes(self):
        tmp = tempfile.mkdtemp()
        cfg = copy.deepcopy(botmod.DEFAULT_CONFIG)
        cfg["alerts"].update({"sound": False, "telegram": False})
        cfg["account"]["cooldown_seconds"] = 0
        b = botmod.MarketBot(
            cfg, out_dir=os.path.join(tmp, "out"), state_dir=os.path.join(tmp, "st"),
        )
        position = b.open_manual_paper_trade(
            "EURUSD", "M30", "BUY", 1.1, 1.099, 1.102,
            "2026-10-24T10:00:00+00:00",
        )
        candles = [{
            "time": "2026-10-24T10:30:00+00:00",
            "open": 1.1, "high": 1.1025, "low": 1.1005, "close": 1.102,
            "volume": 0,
        }]
        with patch.object(botmod.dc, "fetch_candles", return_value=candles):
            errors = b._manage_unconfigured_positions()

        self.assertEqual(errors, [])
        self.assertEqual(position.status, "closed")
        self.assertEqual(position.exit_reason, "take profit")


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
