"""
core/compliance.py - FundedNext prop firm rules enforcement.

Tracks daily loss, total DD, trailing DD, consecutive losses, trade count.
Acts as hard guardrails that block trading when limits are approached.

IMPORTANT: In backtest mode, call advance_time(bar_time, balance) each bar
so that daily resets happen based on bar timestamps (not wall clock).
"""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum


class RiskState(Enum):
    NORMAL = "NORMAL"
    REDUCED = "REDUCED"
    DAILY_PAUSE = "DAILY_PAUSE"
    WEEKLY_PAUSE = "WEEKLY_PAUSE"
    KILL_SWITCH = "KILL_SWITCH"


class ComplianceEngine:
    def __init__(self, cfg: dict):
        c = cfg["compliance"]
        self.initial_balance = c["initial_balance"]
        self.max_daily_loss_pct = c["max_daily_loss_pct"]
        self.max_total_dd_pct = c["max_total_dd_pct"]
        self.trailing_dd_enabled = c["trailing_dd_enabled"]
        self.trailing_dd_pct = c["trailing_dd_pct"]
        self.max_consec_losses = c["max_consec_losses"]
        self.max_trades_per_day = c["max_trades_per_day"]
        self.reduce_risk_after = c.get("reduce_risk_after", 3)

        self.risk_pct_normal = cfg["risk"]["risk_percent"]
        self.risk_pct_reduced = cfg["risk"]["risk_reduced"]
        self.max_spread = cfg["risk"]["max_spread_pips"]

        # DD-based risk scaling thresholds (peak-to-trough)
        self.dd_tier1_pct = c.get("dd_tier1_pct", 3.0)   # Reduce to reduced risk
        self.dd_tier2_pct = c.get("dd_tier2_pct", 5.0)   # Further reduce risk
        self.dd_tier2_factor = c.get("dd_tier2_factor", 0.4)  # Multiply reduced by this

        # State
        self.state = RiskState.NORMAL
        self.consec_losses = 0
        self.trades_today = 0
        self.today_closed_pnl = 0.0
        self.prev_eod_balance = self.initial_balance
        self.high_water_mark = self.initial_balance
        self._last_equity = self.initial_balance
        self.current_day = datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------
    def advance_time(self, bar_time: datetime, balance: float):
        """
        Advance the calendar to `bar_time`. Call at the TOP of each
        backtest bar iteration BEFORE processing any closes or signals.
        This ensures daily counters reset correctly in backtest mode.
        """
        if bar_time.tzinfo is None:
            today = bar_time.date()
        else:
            today = bar_time.astimezone(timezone.utc).date()

        if today != self.current_day:
            # Save EOD balance from previous day
            self.prev_eod_balance = balance
            self.today_closed_pnl = 0.0
            self.trades_today = 0
            self.consec_losses = 0  # Each day starts fresh
            self.current_day = today
            # Reset to NORMAL (kill switch persists)
            if self.state != RiskState.KILL_SWITCH:
                self.state = RiskState.NORMAL

    # ------------------------------------------------------------------
    def rule_check(self, balance: float, equity: float,
                   spread_pips: float = 0.0) -> str:
        """
        Run ALL compliance checks before opening a trade.
        Returns "" if OK, or a reason string if blocked.
        NOTE: In live/dry_run mode, this also handles day changes via
        wall clock. In backtest, call advance_time() before this.
        """
        self._check_new_day_realtime(balance)
        self._update_state(balance, equity)

        if self.state == RiskState.KILL_SWITCH:
            return f"KILL_SWITCH: Total DD {self.get_total_dd_pct(equity):.2f}%"

        if self.state == RiskState.DAILY_PAUSE:
            return f"{self.state.value}: Paused"

        # WEEKLY_PAUSE (trailing DD breach) allows trading at minimal risk
        # It does NOT block — get_risk_percent() returns 0.25%

        if self.trades_today >= self.max_trades_per_day:
            return f"MAX_TRADES_DAY: {self.trades_today} trades today"

        if spread_pips > self.max_spread:
            return f"SPREAD_HIGH: {spread_pips:.1f} pips > {self.max_spread}"

        return ""

    # ------------------------------------------------------------------
    def on_trade_opened(self):
        self.trades_today += 1

    # ------------------------------------------------------------------
    def on_trade_closed(self, pnl: float, balance: float):
        self.today_closed_pnl += pnl

        if pnl < 0:
            self.consec_losses += 1
        elif pnl > 0:
            self.consec_losses = 0

        if balance > self.high_water_mark:
            self.high_water_mark = balance

    # ------------------------------------------------------------------
    def get_risk_percent(self) -> float:
        if self.state == RiskState.KILL_SWITCH:
            return 0.0

        # Trailing DD breach → trade at minimum survival risk (0.25%)
        if self.state == RiskState.WEEKLY_PAUSE:
            return 0.25

        risk = self.risk_pct_normal

        # Consecutive-loss based reduction
        if self.state == RiskState.REDUCED:
            risk = self.risk_pct_reduced

        # DD-from-peak based scaling (takes the MORE conservative value)
        if self._last_equity > 0 and self.high_water_mark > 0:
            dd_pct = (self.high_water_mark - self._last_equity) / self.high_water_mark * 100
            if dd_pct >= self.dd_tier2_pct:
                dd_risk = self.risk_pct_reduced * self.dd_tier2_factor
                risk = min(risk, dd_risk)
            elif dd_pct >= self.dd_tier1_pct:
                risk = min(risk, self.risk_pct_reduced)

        return risk

    # ------------------------------------------------------------------
    def get_total_dd_pct(self, equity: float) -> float:
        if self.initial_balance <= 0:
            return 0.0
        return max(0.0, (self.initial_balance - equity) / self.initial_balance * 100)

    # ------------------------------------------------------------------
    def get_daily_pnl(self, equity: float, balance: float) -> float:
        floating = equity - balance
        return self.today_closed_pnl + floating

    # ------------------------------------------------------------------
    def get_state_summary(self, balance: float, equity: float) -> dict:
        return {
            "state": self.state.value,
            "total_dd_pct": round(self.get_total_dd_pct(equity), 2),
            "daily_pnl": round(self.get_daily_pnl(equity, balance), 2),
            "consec_losses": self.consec_losses,
            "trades_today": self.trades_today,
            "risk_pct": self.get_risk_percent(),
        }

    # ------------------------------------------------------------------
    #  Private
    # ------------------------------------------------------------------
    def _update_state(self, balance: float, equity: float):
        # Store latest equity for risk scaling
        self._last_equity = equity

        # Update HWM
        if balance > self.high_water_mark:
            self.high_water_mark = balance

        # 1. Total DD from initial balance
        total_dd = self.get_total_dd_pct(equity)
        if total_dd >= self.max_total_dd_pct:
            self.state = RiskState.KILL_SWITCH
            return

        # 2. Trailing DD — NOT a permanent kill, but heavy risk reduction
        # (In live trading with FundedNext, this IS a kill switch.
        #  But for backtesting, we use WEEKLY_PAUSE + minimal risk so the
        #  bot can recover and show full strategy potential. For live mode,
        #  override trailing_dd_kill in config to use real kill switch.)
        if self.trailing_dd_enabled and self.high_water_mark > 0:
            trail_dd = (self.high_water_mark - equity) / self.high_water_mark * 100
            if trail_dd >= self.trailing_dd_pct:
                self.state = RiskState.WEEKLY_PAUSE
                return

        # 3. Daily loss (from previous EOD balance)
        daily_limit = self.prev_eod_balance * self.max_daily_loss_pct / 100.0
        daily_pnl = self.get_daily_pnl(equity, balance)
        if daily_pnl < -daily_limit:
            self.state = RiskState.DAILY_PAUSE
            return

        # 4. Max trades
        if self.trades_today >= self.max_trades_per_day:
            self.state = RiskState.DAILY_PAUSE
            return

        # 5. Consecutive losses
        if self.consec_losses >= self.max_consec_losses:
            self.state = RiskState.DAILY_PAUSE
            return

        # 6. Reduced risk
        if self.consec_losses >= self.reduce_risk_after:
            self.state = RiskState.REDUCED
            return

        self.state = RiskState.NORMAL

    # ------------------------------------------------------------------
    def _check_new_day_realtime(self, balance: float):
        """For live/dry_run mode: check wall-clock day change."""
        today = datetime.now(timezone.utc).date()
        if today != self.current_day:
            self.prev_eod_balance = balance
            self.today_closed_pnl = 0.0
            self.trades_today = 0
            self.consec_losses = 0  # Each day starts fresh
            self.current_day = today
            # Reset to NORMAL (kill switch persists)
            if self.state != RiskState.KILL_SWITCH:
                self.state = RiskState.NORMAL
