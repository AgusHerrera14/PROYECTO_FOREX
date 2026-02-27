"""
core/news.py - High-impact news filter using FairEconomy/Forex Factory API.

Blocks trading within a configurable window around high-impact news events.
Fail-safe: if calendar cannot be fetched, block ALL trading.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import requests


class NewsFilter:
    def __init__(self, cfg: dict):
        n = cfg["news"]
        self.enabled = n["enabled"]
        self.pre_minutes = n["pre_minutes"]
        self.post_minutes = n["post_minutes"]
        self.cancel_pendings = n["cancel_pendings"]
        self.move_to_breakeven = n["move_to_breakeven"]
        self.calendar_url = n["calendar_url"]
        self.cache_ttl = n["cache_ttl_seconds"]
        self.fail_safe = n["fail_safe"]

        self._events: list[dict] = []
        self._last_fetch: float = 0
        self._fetch_ok: bool = False

        self._cache_file = Path(cfg["logging"]["log_dir"]) / "news_cache.json"
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def refresh(self):
        """Fetch calendar from API (or cache)."""
        if not self.enabled:
            return

        now = time.time()
        if self._last_fetch > 0 and (now - self._last_fetch) < self.cache_ttl:
            return  # Cache still valid

        try:
            resp = requests.get(self.calendar_url, timeout=10)
            resp.raise_for_status()
            all_events = resp.json()

            # Filter: HIGH impact, EUR or USD
            self._events = [
                e for e in all_events
                if e.get("impact", "").lower() == "high"
                and e.get("country", "").upper() in ("USD", "EUR", "ALL")
            ]

            self._last_fetch = now
            self._fetch_ok = True

            # Cache to disk
            with open(self._cache_file, "w") as f:
                json.dump({"fetched": now, "events": self._events}, f)

        except Exception as e:
            # Try loading from disk cache
            if self._cache_file.exists():
                try:
                    with open(self._cache_file) as f:
                        cached = json.load(f)
                    self._events = cached.get("events", [])
                    self._fetch_ok = True
                    self._last_fetch = now
                except Exception:
                    self._fetch_ok = False
            else:
                self._fetch_ok = False

    # ------------------------------------------------------------------
    def is_blocked(self) -> bool:
        """Is current time within a news window? (= block trading)"""
        if not self.enabled:
            return False

        # Fail-safe: can't verify news -> block
        if self.fail_safe and not self._fetch_ok:
            return True

        now = datetime.now(timezone.utc)

        for event in self._events:
            event_time = self._parse_event_time(event)
            if event_time is None:
                continue

            window_start = event_time - timedelta(minutes=self.pre_minutes)
            window_end = event_time + timedelta(minutes=self.post_minutes)

            if window_start <= now <= window_end:
                return True

        return False

    # ------------------------------------------------------------------
    def should_cancel_pendings(self) -> bool:
        return self.cancel_pendings and self.is_blocked()

    def should_move_to_breakeven(self) -> bool:
        return self.move_to_breakeven and self.is_blocked()

    # ------------------------------------------------------------------
    def get_status(self) -> str:
        if not self.enabled:
            return "DISABLED"
        if not self._fetch_ok:
            return "FAILSAFE_BLOCK"
        if self.is_blocked():
            next_ev = self.get_next_event()
            name = next_ev.get("title", "unknown") if next_ev else "unknown"
            return f"BLOCKED|{name}"
        return "CLEAR"

    # ------------------------------------------------------------------
    def get_next_event(self) -> Optional[dict]:
        """Get the next upcoming high-impact event."""
        now = datetime.now(timezone.utc)
        nearest = None
        nearest_time = None

        for event in self._events:
            et = self._parse_event_time(event)
            if et is None or et < now - timedelta(minutes=self.post_minutes):
                continue
            if nearest_time is None or et < nearest_time:
                nearest_time = et
                nearest = event

        return nearest

    # ------------------------------------------------------------------
    def minutes_to_next(self) -> int:
        """Minutes until next high-impact event."""
        nxt = self.get_next_event()
        if nxt is None:
            return 9999
        et = self._parse_event_time(nxt)
        if et is None:
            return 9999
        diff = (et - datetime.now(timezone.utc)).total_seconds() / 60
        return max(0, int(diff))

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_event_time(event: dict) -> Optional[datetime]:
        """Parse event time from various formats."""
        date_str = event.get("date", "")
        if not date_str:
            return None
        try:
            # FairEconomy format: "2024-01-05T13:30:00-05:00"
            dt = datetime.fromisoformat(date_str)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None
