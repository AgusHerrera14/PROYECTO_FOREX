"""
ops/logger.py - CSV trade logging + system log + daily summary.
"""
from __future__ import annotations
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path


class TradeLogger:
    def __init__(self, cfg: dict):
        log_cfg = cfg["logging"]
        self.log_dir = Path(log_cfg["log_dir"])
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.trade_csv_enabled = log_cfg["trade_csv"]
        self.verbose = log_cfg["verbose"]

        # System logger (Python logging)
        self._setup_system_logger()

        # Trade CSV file
        self._trade_file = None
        self._trade_writer = None
        if self.trade_csv_enabled:
            self._open_trade_csv()

        # Daily stats
        self._day_trades = 0
        self._day_wins = 0
        self._day_pnl = 0.0
        self._day_max_dd = 0.0
        self._current_day = datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------
    #  Trade CSV
    # ------------------------------------------------------------------
    def log_trade_open(self, ticket: int, strategy: str, direction: str,
                       entry: float, sl: float, tp: float, lots: float,
                       risk_pct: float, spread: float, news_status: str,
                       reason: str, equity: float, balance: float, dd_pct: float):
        self._write_trade_row({
            "timestamp": self._ts(),
            "event": "OPEN",
            "ticket": ticket,
            "strategy": strategy,
            "direction": direction,
            "entry": f"{entry:.5f}",
            "sl": f"{sl:.5f}",
            "tp": f"{tp:.5f}",
            "lots": f"{lots:.2f}",
            "risk_pct": f"{risk_pct:.2f}",
            "spread_pips": f"{spread:.1f}",
            "news_status": news_status,
            "reason": reason,
            "pnl": "",
            "equity": f"{equity:.2f}",
            "balance": f"{balance:.2f}",
            "dd_pct": f"{dd_pct:.2f}",
        })

    def log_trade_close(self, ticket: int, strategy: str, direction: str,
                        entry: float, exit_price: float, lots: float,
                        pnl: float, reason: str, equity: float, balance: float):
        self._write_trade_row({
            "timestamp": self._ts(),
            "event": "CLOSE",
            "ticket": ticket,
            "strategy": strategy,
            "direction": direction,
            "entry": f"{entry:.5f}",
            "sl": "",
            "tp": "",
            "lots": f"{lots:.2f}",
            "risk_pct": "",
            "spread_pips": "",
            "news_status": "",
            "reason": reason,
            "pnl": f"{pnl:.2f}",
            "equity": f"{equity:.2f}",
            "balance": f"{balance:.2f}",
            "dd_pct": "",
        })

        # Update daily stats
        self._day_trades += 1
        self._day_pnl += pnl
        if pnl > 0:
            self._day_wins += 1

    # ------------------------------------------------------------------
    #  System log
    # ------------------------------------------------------------------
    def info(self, msg: str):
        self._syslog.info(msg)

    def warning(self, msg: str):
        self._syslog.warning(msg)

    def error(self, msg: str):
        self._syslog.error(msg)

    def debug(self, msg: str):
        if self.verbose:
            self._syslog.debug(msg)

    # ------------------------------------------------------------------
    #  Daily summary
    # ------------------------------------------------------------------
    def write_daily_summary(self):
        wr = (self._day_wins / self._day_trades * 100) if self._day_trades > 0 else 0
        msg = (f"DAILY SUMMARY | Trades: {self._day_trades} | "
               f"Wins: {self._day_wins} | WR: {wr:.1f}% | "
               f"PnL: ${self._day_pnl:.2f}")
        self.info(msg)

        # Reset for new day
        self._day_trades = 0
        self._day_wins = 0
        self._day_pnl = 0.0

    def check_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self._current_day:
            self.write_daily_summary()
            self._current_day = today
            self._open_trade_csv()  # New file for new month

    # ------------------------------------------------------------------
    def flush(self):
        if self._trade_file:
            self._trade_file.flush()

    def close(self):
        self.write_daily_summary()
        if self._trade_file:
            self._trade_file.close()
            self._trade_file = None

    # ------------------------------------------------------------------
    #  Private
    # ------------------------------------------------------------------
    _TRADE_HEADERS = [
        "timestamp", "event", "ticket", "strategy", "direction",
        "entry", "sl", "tp", "lots", "risk_pct", "spread_pips",
        "news_status", "reason", "pnl", "equity", "balance", "dd_pct"
    ]

    def _open_trade_csv(self):
        if not self.trade_csv_enabled:
            return
        month = datetime.now(timezone.utc).strftime("%Y%m")
        path = self.log_dir / f"trades_{month}.csv"
        is_new = not path.exists()

        if self._trade_file:
            self._trade_file.close()

        self._trade_file = open(path, "a", newline="", encoding="utf-8")
        self._trade_writer = csv.DictWriter(self._trade_file,
                                            fieldnames=self._TRADE_HEADERS)
        if is_new:
            self._trade_writer.writeheader()

    def _write_trade_row(self, row: dict):
        if not self.trade_csv_enabled or not self._trade_writer:
            return
        self._trade_writer.writerow(row)
        self._trade_file.flush()

    def _setup_system_logger(self):
        self._syslog = logging.getLogger("FN_Bot")
        self._syslog.setLevel(logging.DEBUG if self.verbose else logging.INFO)

        # File handler
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        fh = logging.FileHandler(self.log_dir / f"system_{day}.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)

        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)

        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)

        if not self._syslog.handlers:
            self._syslog.addHandler(fh)
            self._syslog.addHandler(ch)

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
