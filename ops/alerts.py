"""
ops/alerts.py - Telegram bot notifications.

Sends alerts for: trade opened/closed, rule breach, safe mode, news lock.
Uses plain requests (no extra dependencies).
"""
from __future__ import annotations
import requests
from datetime import datetime, timezone


class TelegramAlerts:
    def __init__(self, cfg: dict):
        t = cfg["telegram"]
        self.enabled = t["enabled"]
        self.bot_token = t["bot_token"]
        self.chat_id = t["chat_id"]
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}"

    # ------------------------------------------------------------------
    def send(self, message: str):
        """Send a plain text message to Telegram."""
        if not self.enabled:
            return
        if not self.bot_token or self.bot_token == "YOUR_BOT_TOKEN_HERE":
            return
        try:
            url = f"{self._base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": f"🤖 FN Bot | {message}",
                "parse_mode": "HTML",
            }
            requests.post(url, json=payload, timeout=10)
        except Exception:
            pass  # Never crash for alert failure

    # ------------------------------------------------------------------
    def trade_opened(self, direction: str, lots: float, entry: float,
                     sl: float, tp: float, sl_pips: float, reason: str):
        emoji = "🟢" if direction == "BUY" else "🔴"
        msg = (f"{emoji} <b>{direction}</b> EURUSD\n"
               f"Lots: {lots:.2f} | Entry: {entry:.5f}\n"
               f"SL: {sl:.5f} ({sl_pips:.1f} pips) | TP: {tp:.5f}\n"
               f"Reason: {reason}")
        self.send(msg)

    def trade_closed(self, direction: str, pnl: float, reason: str):
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (f"{emoji} CLOSED {direction} EURUSD\n"
               f"PnL: ${pnl:.2f} | Reason: {reason}")
        self.send(msg)

    def rule_breach(self, rule: str, details: str):
        msg = f"⚠️ <b>RULE BREACH</b>: {rule}\n{details}"
        self.send(msg)

    def safe_mode(self, reason: str):
        msg = f"🛑 <b>SAFE MODE ACTIVATED</b>\n{reason}"
        self.send(msg)

    def news_lock(self, event_name: str, minutes: int):
        msg = f"📰 <b>NEWS LOCK</b>: {event_name} in {minutes} min"
        self.send(msg)

    def heartbeat(self, balance: float, equity: float, dd_pct: float,
                  trades_today: int, state: str):
        msg = (f"💓 Heartbeat\n"
               f"Bal: ${balance:.2f} | Eq: ${equity:.2f}\n"
               f"DD: {dd_pct:.2f}% | Trades today: {trades_today}\n"
               f"State: {state}")
        self.send(msg)

    def bot_started(self, mode: str, balance: float):
        msg = f"🚀 Bot started in <b>{mode}</b> mode\nBalance: ${balance:.2f}"
        self.send(msg)

    def bot_stopped(self, reason: str):
        msg = f"🔴 Bot stopped: {reason}"
        self.send(msg)
