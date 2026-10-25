"""
Alerts: console + log file always; Windows sound and Telegram when available.

Telegram: create a bot with @BotFather, then put these in .env
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=your_chat_id
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import winsound  # Windows only
except ImportError:
    winsound = None


class Alerter:
    def __init__(self, log_path: str, sound: bool = True, telegram: bool = True,
                 dedupe_seconds: int = 900):
        self.log_path = log_path
        self.sound = sound and winsound is not None
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN") if telegram else None
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID") if telegram else None
        self.dedupe_seconds = dedupe_seconds
        self._seen: dict[str, float] = {}
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    @property
    def channels(self) -> list[str]:
        ch = ["console", "log"]
        if self.sound:
            ch.append("sound")
        if self.tg_token and self.tg_chat:
            ch.append("telegram")
        return ch

    def send(self, title: str, body: str = "", level: str = "info", key: str | None = None) -> bool:
        """Send an alert. Returns False if suppressed as a duplicate."""
        now = time.time()
        if key:
            if now - self._seen.get(key, 0) < self.dedupe_seconds:
                return False
            self._seen[key] = now
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        icon = {"trade": "🟢", "warn": "⚠️", "error": "❌", "close": "🏁"}.get(level, "🔔")
        line = f"[{ts}] {icon} {title}" + (f"\n    {body}" if body else "")
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # old Windows consoles
            print(line.encode("ascii", "replace").decode(), flush=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": ts, "level": level, "title": title, "body": body}) + "\n")
        if self.sound and level in ("trade", "close", "warn"):
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        if self.tg_token and self.tg_chat:
            self._telegram(f"{icon} {title}\n{body}".strip())
        return True

    def _telegram(self, text: str) -> None:
        try:
            data = urllib.parse.urlencode({"chat_id": self.tg_chat, "text": text[:4000]}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage", data=data)
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"    (telegram send failed: {e.__class__.__name__})")
