"""
News blackout filter
====================
Blocks new entries around high-impact economic releases for the currencies in
a symbol (EURUSD -> EUR, USD; XAUUSD -> USD). Uses the free Forex Factory
weekly calendar feed, cached to disk for an hour. If the feed cannot be
reached the filter fails open and says so in its notes.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_UA = "Mozilla/5.0 market-analysis-bot/1.0"

_SYMBOL_CCY = {"XAUUSD": ["USD"], "XAGUSD": ["USD"], "US30": ["USD"],
               "NAS100": ["USD"], "SPX500": ["USD"], "BTCUSD": ["USD"]}


def currencies_for(symbol: str) -> list[str]:
    s = symbol.upper()
    if s in _SYMBOL_CCY:
        return _SYMBOL_CCY[s]
    if len(s) == 6 and s.isalpha():
        return [s[:3], s[3:]]
    return ["USD"]


def parse_events(raw: list[dict]) -> list[dict]:
    out = []
    for e in raw:
        try:
            dt = datetime.fromisoformat(str(e.get("date")))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append({"title": e.get("title", ""), "currency": str(e.get("country", "")).upper(),
                    "impact": str(e.get("impact", "")), "time": dt.astimezone(timezone.utc)})
    return out


class NewsFilter:
    def __init__(self, cache_path: str, window_minutes: int = 30,
                 impacts: tuple = ("High",), enabled: bool = True, ttl_seconds: int = 3600):
        self.cache_path = cache_path
        self.window = timedelta(minutes=window_minutes)
        self.impacts = set(impacts)
        self.enabled = enabled
        self.ttl = ttl_seconds
        self.events: list[dict] = []
        self.loaded_at = 0.0
        self.error: Optional[str] = None

    def _load(self) -> None:
        if time.time() - self.loaded_at < self.ttl and self.events:
            return
        raw = None
        if os.path.exists(self.cache_path) and time.time() - os.path.getmtime(self.cache_path) < self.ttl:
            try:
                with open(self.cache_path, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = None
        if raw is None:
            try:
                req = urllib.request.Request(FEED_URL, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                with open(self.cache_path, "w", encoding="utf-8") as fh:
                    json.dump(raw, fh)
                self.error = None
            except Exception as e:  # fail open
                self.error = f"calendar unavailable ({e.__class__.__name__}) — news filter skipped"
                if os.path.exists(self.cache_path):
                    try:
                        with open(self.cache_path, encoding="utf-8") as fh:
                            raw = json.load(fh)
                    except (OSError, ValueError):
                        raw = []
                else:
                    raw = []
        self.events = parse_events(raw or [])
        self.loaded_at = time.time()

    def check(self, symbol: str, now: Optional[datetime] = None) -> dict:
        """Returns {'blocked': bool, 'events': [...], 'note': str|None}."""
        if not self.enabled:
            return {"blocked": False, "events": [], "note": "news filter disabled"}
        self._load()
        now = now or datetime.now(timezone.utc)
        ccys = set(currencies_for(symbol))
        hits = [e for e in self.events
                if e["currency"] in ccys and e["impact"] in self.impacts
                and abs(e["time"] - now) <= self.window]
        return {"blocked": bool(hits),
                "events": [{"title": e["title"], "currency": e["currency"],
                            "time": e["time"].isoformat()} for e in hits],
                "note": self.error}

    def upcoming(self, symbols: list[str], hours: int = 24) -> list[dict]:
        if not self.enabled:
            return []
        self._load()
        now = datetime.now(timezone.utc)
        ccys = {c for s in symbols for c in currencies_for(s)}
        evs = [e for e in self.events if e["currency"] in ccys and e["impact"] in self.impacts
               and now - self.window <= e["time"] <= now + timedelta(hours=hours)]
        return [{"title": e["title"], "currency": e["currency"], "time": e["time"].isoformat()}
                for e in sorted(evs, key=lambda x: x["time"])]
