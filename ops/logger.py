"""
ops/logger.py - CSV trade logging + system log + daily summary + Excel report.
"""
from __future__ import annotations
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ops.excel_report import ExcelReporter


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

        # Excel reporter
        self._excel = ExcelReporter(cfg)

        # Open-trade cache: ticket -> open data (for pairing open/close)
        self._open_trades: dict[int, dict] = {}

        # Daily stats
        self._day_trades = 0
        self._day_wins = 0
        self._day_pnl = 0.0
        self._day_max_dd = 0.0
        self._current_day = datetime.now(timezone.utc).date()

        # Track balance/peak for DD calculation in Excel summary
        self._peak_balance = cfg.get("compliance", {}).get("initial_balance", 100000.0)

    # ------------------------------------------------------------------
    #  Trade CSV
    # ------------------------------------------------------------------
    def log_trade_open(self, ticket: int, strategy: str, direction: str,
                       entry: float, sl: float, tp: float, lots: float,
                       risk_pct: float, spread: float, news_status: str,
                       reason: str, equity: float, balance: float, dd_pct: float):
        now = datetime.now(timezone.utc)
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

        # Cache open data for Excel pairing
        from core.indicators import price_to_pips
        sl_pips = round(price_to_pips(abs(entry - sl)), 1)
        tp_pips = round(price_to_pips(abs(tp - entry)), 1)
        self._open_trades[ticket] = {
            "open_time": now,
            "strategy": strategy,
            "direction": direction,
            "entry": round(entry, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "lots": round(lots, 2),
            "risk_pct": round(risk_pct, 2),
            "sl_pips": sl_pips,
            "tp_pips": tp_pips,
            "spread": round(spread, 1),
            "news_status": news_status,
            "signal_reason": reason,
        }

    def log_trade_close(self, ticket: int, strategy: str, direction: str,
                        entry: float, exit_price: float, lots: float,
                        pnl: float, reason: str, equity: float, balance: float):
        now = datetime.now(timezone.utc)
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

        # Track peak balance for DD
        self._peak_balance = max(self._peak_balance, balance)

        # Build Excel row from cached open data + close data
        open_data = self._open_trades.pop(ticket, {})
        open_time = open_data.get("open_time", now)
        duration = now - open_time
        hours = int(duration.total_seconds() // 3600)
        mins = int((duration.total_seconds() % 3600) // 60)
        duration_str = f"{hours}h {mins}m"

        self._excel.record_trade({
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "ticket": ticket,
            "strategy": open_data.get("strategy", strategy),
            "direction": open_data.get("direction", direction),
            "entry": open_data.get("entry", round(entry, 5)),
            "sl": open_data.get("sl", ""),
            "tp": open_data.get("tp", ""),
            "exit_price": round(exit_price, 5),
            "lots": open_data.get("lots", round(lots, 2)),
            "risk_pct": open_data.get("risk_pct", ""),
            "sl_pips": open_data.get("sl_pips", ""),
            "tp_pips": open_data.get("tp_pips", ""),
            "spread": open_data.get("spread", ""),
            "pnl": round(pnl, 2),
            "duration": duration_str,
            "close_reason": reason,
            "news_status": open_data.get("news_status", ""),
            "signal_reason": open_data.get("signal_reason", ""),
            "equity": round(equity, 2),
            "balance": round(balance, 2),
            "dd_pct": round(max(0, (self._peak_balance - balance) / self._peak_balance * 100), 2),
        })

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
    def write_daily_summary(self, balance: float | None = None):
        wr = (self._day_wins / self._day_trades * 100) if self._day_trades > 0 else 0
        msg = (f"DAILY SUMMARY | Trades: {self._day_trades} | "
               f"Wins: {self._day_wins} | WR: {wr:.1f}% | "
               f"PnL: ${self._day_pnl:.2f}")
        self.info(msg)

        # Export to Excel
        if self._excel.has_pending_trades():
            bal = balance if balance is not None else 0
            self._excel.export_daily(bal, self._peak_balance)
            self.info(f"Excel report updated: {self._excel.filepath}")

        # Reset for new day
        self._day_trades = 0
        self._day_wins = 0
        self._day_pnl = 0.0

    def check_new_day(self, balance: float | None = None):
        today = datetime.now(timezone.utc).date()
        if today != self._current_day:
            self.write_daily_summary(balance)
            self._current_day = today
            self._open_trade_csv()  # New file for new month

    # ------------------------------------------------------------------
    def flush(self):
        if self._trade_file:
            self._trade_file.flush()

    def close(self, balance: float | None = None):
        self.write_daily_summary(balance)
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
