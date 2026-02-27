"""
broker/mt5_gateway.py - Unified broker gateway.

Two implementations behind the same interface:
  - LiveGateway:   Connects to MT5 via MetaTrader5 Python package (Windows only)
  - DryRunGateway: Simulates order execution for Mac testing & backtesting

The caller (main.py) uses the same API regardless of mode.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from core.indicators import price_to_pips, pip_size


# ===================================================================
# Position data class (shared by both gateways)
# ===================================================================
@dataclass
class Position:
    ticket: int
    direction: int        # +1 BUY, -1 SELL
    entry: float
    sl: float
    tp: float
    lots: float
    strategy: str
    open_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sl_pips: float = 0.0
    be_activated: bool = False
    partial_closed: bool = False


@dataclass
class AccountInfo:
    balance: float = 0.0
    equity: float = 0.0
    margin_free: float = 0.0


@dataclass
class TradeResult:
    success: bool
    ticket: int = 0
    price: float = 0.0
    error: str = ""


# ===================================================================
# Abstract interface
# ===================================================================
class BrokerGateway:
    def connect(self) -> bool: ...
    def disconnect(self): ...
    def is_connected(self) -> bool: ...
    def get_account_info(self) -> AccountInfo: ...
    def get_ask(self) -> float: ...
    def get_bid(self) -> float: ...
    def get_spread_pips(self) -> float: ...
    def has_position(self) -> bool: ...
    def get_positions(self) -> list[Position]: ...
    def open_trade(self, direction: int, lots: float, sl: float, tp: float,
                   comment: str = "") -> TradeResult: ...
    def modify_sl(self, ticket: int, new_sl: float) -> bool: ...
    def close_position(self, ticket: int, reason: str = "") -> Optional[float]: ...
    def close_all(self, reason: str = ""): ...
    def cancel_pending(self, reason: str = ""): ...


# ===================================================================
# DRY RUN GATEWAY (Mac / Backtest)
# ===================================================================
class DryRunGateway(BrokerGateway):
    """Simulates broker execution. Tracks virtual balance and positions."""

    def __init__(self, cfg: dict):
        init_bal = cfg["compliance"]["initial_balance"]
        self.balance = init_bal
        self.equity = init_bal
        self.positions: dict[int, Position] = {}
        self._next_ticket = 1000
        self.trade_history: list[dict] = []
        self._connected = False
        self._last_ask = 0.0
        self._last_bid = 0.0
        self._spread_pips = cfg.get("backtest", {}).get("spread_pips", 1.0)
        self._slippage_pips = cfg.get("backtest", {}).get("slippage_pips", 0.5)
        self.pip_value_per_lot = cfg["risk"]["pip_value_per_lot"]

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self):
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_account_info(self) -> AccountInfo:
        self._update_equity()
        return AccountInfo(
            balance=round(self.balance, 2),
            equity=round(self.equity, 2),
            margin_free=round(self.equity, 2),
        )

    def get_ask(self) -> float:
        return self._last_ask

    def get_bid(self) -> float:
        return self._last_bid

    def get_spread_pips(self) -> float:
        return self._spread_pips

    def update_price(self, bid: float, ask: float = 0):
        """Update current price (called by data_feed or backtest loop)."""
        self._last_bid = bid
        self._last_ask = ask if ask > 0 else bid + self._spread_pips * pip_size()

    def has_position(self) -> bool:
        return len(self.positions) > 0

    def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    def open_trade(self, direction: int, lots: float, sl: float, tp: float,
                   comment: str = "") -> TradeResult:
        if lots <= 0:
            return TradeResult(success=False, error="Invalid lot size")

        # Simulate fill with slippage
        slip = self._slippage_pips * pip_size()
        if direction > 0:
            fill_price = self._last_ask + slip
        else:
            fill_price = self._last_bid - slip

        ticket = self._next_ticket
        self._next_ticket += 1

        sl_pips = price_to_pips(abs(fill_price - sl))

        pos = Position(
            ticket=ticket,
            direction=direction,
            entry=round(fill_price, 5),
            sl=sl,
            tp=tp,
            lots=lots,
            strategy=comment,
            sl_pips=sl_pips,
        )
        self.positions[ticket] = pos
        return TradeResult(success=True, ticket=ticket, price=fill_price)

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        if ticket in self.positions:
            self.positions[ticket].sl = round(new_sl, 5)
            return True
        return False

    def close_position(self, ticket: int, reason: str = "") -> Optional[float]:
        if ticket not in self.positions:
            return None

        pos = self.positions[ticket]
        if pos.direction > 0:
            exit_price = self._last_bid
        else:
            exit_price = self._last_ask

        pnl = self._calc_pnl(pos, exit_price)
        self.balance += pnl
        self._record_close(pos, exit_price, pnl, reason)
        del self.positions[ticket]
        return pnl

    def close_all(self, reason: str = ""):
        for ticket in list(self.positions.keys()):
            self.close_position(ticket, reason)

    def cancel_pending(self, reason: str = ""):
        pass  # No pending orders in dry-run

    # --- Backtest bar processing ---
    def check_sl_tp(self, bar: dict, cfg: dict = None):
        """
        Check if SL or TP was hit during a bar (for backtesting).
        bar: dict with keys 'high', 'low', 'open', 'close' (and optionally 'atr').
        cfg: if provided, simulate breakeven and trailing stop management.

        Order of processing (per bar):
        1. Check if breakeven target was reached → move SL to entry+1pip
        2. Check if trailing stop should advance
        3. Check SL/TP with updated stops
        """
        ps = pip_size()

        for ticket in list(self.positions.keys()):
            pos = self.positions[ticket]

            # --- Partial close simulation ---
            if cfg is not None and not pos.partial_closed and pos.sl_pips > 0:
                tm = cfg["trade_management"]
                if tm.get("partial_close_enabled", False):
                    pc_r = tm.get("partial_close_r", 1.0)
                    pc_pct = tm.get("partial_close_pct", 50.0) / 100.0
                    pc_dist = pos.sl_pips * ps * pc_r
                    pc_hit = False
                    if pos.direction > 0:
                        pc_target = pos.entry + pc_dist
                        if bar["high"] >= pc_target:
                            pc_hit = True
                    else:
                        pc_target = pos.entry - pc_dist
                        if bar["low"] <= pc_target:
                            pc_hit = True

                    if pc_hit:
                        # Close partial_close_pct of the position at the target
                        close_lots = round(pos.lots * pc_pct, 2)
                        remain_lots = round(pos.lots - close_lots, 2)
                        if close_lots > 0 and remain_lots > 0:
                            # Record partial close as a realized trade
                            pc_pnl = self._calc_pnl_at(pos, pc_target, close_lots)
                            self.balance += pc_pnl
                            self._record_partial_close(pos, pc_target, pc_pnl,
                                                       close_lots, "PARTIAL_1R")
                            # Update position: reduce lots, move SL to BE
                            pos.lots = remain_lots
                            pos.partial_closed = True
                            if pos.direction > 0:
                                pos.sl = round(pos.entry + ps, 5)
                            else:
                                pos.sl = round(pos.entry - ps, 5)
                            pos.be_activated = True

            # --- Breakeven simulation (only if partial close not active) ---
            if cfg is not None and not pos.be_activated:
                tm = cfg["trade_management"]
                if tm["breakeven_enabled"] and pos.sl_pips > 0:
                    # Price that position must reach for breakeven activation
                    be_dist = pos.sl_pips * ps * tm["breakeven_r"]
                    if pos.direction > 0:
                        be_target = pos.entry + be_dist
                        if bar["high"] >= be_target:
                            new_sl = pos.entry + ps  # 1 pip above entry
                            if new_sl > pos.sl:
                                pos.sl = round(new_sl, 5)
                                pos.be_activated = True
                    else:
                        be_target = pos.entry - be_dist
                        if bar["low"] <= be_target:
                            new_sl = pos.entry - ps  # 1 pip below entry
                            if new_sl < pos.sl:
                                pos.sl = round(new_sl, 5)
                                pos.be_activated = True

            # --- Trailing stop simulation ---
            if cfg is not None and pos.be_activated:
                tm = cfg["trade_management"]
                atr = bar.get("atr", 0)
                if tm["trailing_enabled"] and atr > 0 and pos.sl_pips > 0:
                    trail_dist_price = pos.sl_pips * ps * tm["trailing_start_r"]
                    trail_atr = atr * tm["trailing_atr_mult"]
                    if pos.direction > 0:
                        trail_target = pos.entry + trail_dist_price
                        if bar["high"] >= trail_target:
                            # Trail from the highest price in this bar
                            new_sl = bar["high"] - trail_atr
                            if new_sl > pos.sl and new_sl > pos.entry:
                                pos.sl = round(new_sl, 5)
                    else:
                        trail_target = pos.entry - trail_dist_price
                        if bar["low"] <= trail_target:
                            new_sl = bar["low"] + trail_atr
                            if new_sl < pos.sl and new_sl < pos.entry:
                                pos.sl = round(new_sl, 5)

            # --- SL/TP check (with potentially updated SL) ---
            hit = False
            exit_price = 0.0
            reason = ""

            if pos.direction > 0:  # BUY
                if bar["low"] <= pos.sl:
                    hit, exit_price, reason = True, pos.sl, "SL_HIT"
                    if pos.be_activated:
                        reason = "BE_HIT" if abs(pos.sl - pos.entry) < ps * 3 else "TRAIL_HIT"
                elif bar["high"] >= pos.tp:
                    hit, exit_price, reason = True, pos.tp, "TP_HIT"
            else:  # SELL
                if bar["high"] >= pos.sl:
                    hit, exit_price, reason = True, pos.sl, "SL_HIT"
                    if pos.be_activated:
                        reason = "BE_HIT" if abs(pos.sl - pos.entry) < ps * 3 else "TRAIL_HIT"
                elif bar["low"] <= pos.tp:
                    hit, exit_price, reason = True, pos.tp, "TP_HIT"

            if hit:
                pnl = self._calc_pnl(pos, exit_price)
                self.balance += pnl
                self._record_close(pos, exit_price, pnl, reason)
                del self.positions[ticket]

    # --- Private ---
    def _calc_pnl(self, pos: Position, exit_price: float) -> float:
        if pos.direction > 0:
            pips = (exit_price - pos.entry) / pip_size()
        else:
            pips = (pos.entry - exit_price) / pip_size()
        return pips * self.pip_value_per_lot * pos.lots

    def _calc_pnl_at(self, pos: Position, exit_price: float, lots: float) -> float:
        """Calculate PnL for a specific lot size (used for partial closes)."""
        if pos.direction > 0:
            pips = (exit_price - pos.entry) / pip_size()
        else:
            pips = (pos.entry - exit_price) / pip_size()
        return pips * self.pip_value_per_lot * lots

    def _update_equity(self):
        floating = 0.0
        for pos in self.positions.values():
            if pos.direction > 0:
                cur = self._last_bid
                pips = (cur - pos.entry) / pip_size() if pip_size() > 0 else 0
            else:
                cur = self._last_ask
                pips = (pos.entry - cur) / pip_size() if pip_size() > 0 else 0
            floating += pips * self.pip_value_per_lot * pos.lots
        self.equity = self.balance + floating

    def _record_partial_close(self, pos: Position, exit_price: float, pnl: float,
                              lots: float, reason: str):
        """Record a partial close as a separate trade entry."""
        self.trade_history.append({
            "ticket": pos.ticket,
            "direction": "BUY" if pos.direction > 0 else "SELL",
            "entry": pos.entry,
            "exit": exit_price,
            "sl": pos.sl,
            "tp": pos.tp,
            "lots": lots,
            "pnl": round(pnl, 2),
            "reason": reason,
            "strategy": pos.strategy,
            "open_time": pos.open_time.isoformat(),
            "close_time": datetime.now(timezone.utc).isoformat(),
        })

    def _record_close(self, pos: Position, exit_price: float, pnl: float, reason: str):
        self.trade_history.append({
            "ticket": pos.ticket,
            "direction": "BUY" if pos.direction > 0 else "SELL",
            "entry": pos.entry,
            "exit": exit_price,
            "sl": pos.sl,
            "tp": pos.tp,
            "lots": pos.lots,
            "pnl": round(pnl, 2),
            "reason": reason,
            "strategy": pos.strategy,
            "open_time": pos.open_time.isoformat(),
            "close_time": datetime.now(timezone.utc).isoformat(),
        })


# ===================================================================
# LIVE GATEWAY (Windows VPS with MT5)
# ===================================================================
class LiveGateway(BrokerGateway):
    """Connects to MetaTrader 5 terminal via official Python package."""

    def __init__(self, cfg: dict):
        self.mt5_path = cfg["mt5"]["path"]
        self.login = cfg["mt5"]["login"]
        self.password = cfg["mt5"]["password"]
        self.server = cfg["mt5"]["server"]
        self.timeout = cfg["mt5"].get("timeout", 10000)
        self.symbol = cfg["symbol"]
        self.magic = 202503
        self._connected = False
        self.pip_value_per_lot = cfg["risk"]["pip_value_per_lot"]
        self.mt5 = None

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self.mt5 = mt5
        except ImportError:
            print("[MT5] ERROR: MetaTrader5 package not installed. Run: pip install MetaTrader5")
            return False

        if not self.mt5.initialize(path=self.mt5_path, timeout=self.timeout):
            print(f"[MT5] initialize() failed: {self.mt5.last_error()}")
            return False

        if self.login and self.password:
            auth = self.mt5.login(self.login, password=self.password, server=self.server)
            if not auth:
                print(f"[MT5] login() failed: {self.mt5.last_error()}")
                return False

        # Ensure symbol is available
        if not self.mt5.symbol_select(self.symbol, True):
            print(f"[MT5] Symbol {self.symbol} not available")
            return False

        info = self.mt5.account_info()
        if info:
            print(f"[MT5] Connected. Account: {info.login} | Balance: ${info.balance:.2f}")
        self._connected = True
        return True

    def disconnect(self):
        if self.mt5:
            self.mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        if not self._connected or not self.mt5:
            return False
        info = self.mt5.account_info()
        return info is not None

    def get_account_info(self) -> AccountInfo:
        info = self.mt5.account_info()
        if info is None:
            return AccountInfo()
        return AccountInfo(
            balance=info.balance,
            equity=info.equity,
            margin_free=info.margin_free,
        )

    def get_ask(self) -> float:
        tick = self.mt5.symbol_info_tick(self.symbol)
        return tick.ask if tick else 0

    def get_bid(self) -> float:
        tick = self.mt5.symbol_info_tick(self.symbol)
        return tick.bid if tick else 0

    def get_spread_pips(self) -> float:
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return 999.0
        return (tick.ask - tick.bid) / pip_size()

    def has_position(self) -> bool:
        positions = self.mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return False
        return any(p.magic == self.magic for p in positions)

    def get_positions(self) -> list[Position]:
        positions = self.mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return []
        result = []
        for p in positions:
            if p.magic != self.magic:
                continue
            result.append(Position(
                ticket=p.ticket,
                direction=1 if p.type == 0 else -1,  # 0=BUY, 1=SELL
                entry=p.price_open,
                sl=p.sl,
                tp=p.tp,
                lots=p.volume,
                strategy=p.comment or "",
            ))
        return result

    def open_trade(self, direction: int, lots: float, sl: float, tp: float,
                   comment: str = "") -> TradeResult:
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return TradeResult(success=False, error="No tick data")

        if direction > 0:
            trade_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            trade_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lots,
            "type": trade_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 30,
            "magic": self.magic,
            "comment": f"FN_{comment}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        for attempt in range(3):
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                return TradeResult(success=True, ticket=result.order, price=result.price)
            time.sleep(0.5)

        err = result.comment if result else "Unknown error"
        return TradeResult(success=False, error=f"Failed after 3 retries: {err}")

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        mt5 = self.mt5
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return False
        p = pos[0]

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": ticket,
            "sl": new_sl,
            "tp": p.tp,
            "magic": self.magic,
        }
        result = mt5.order_send(request)
        return result and result.retcode == mt5.TRADE_RETCODE_DONE

    def close_position(self, ticket: int, reason: str = "") -> Optional[float]:
        mt5 = self.mt5
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return None
        p = pos[0]

        trade_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(self.symbol)
        price = tick.bid if p.type == 0 else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": p.volume,
            "type": trade_type,
            "position": ticket,
            "price": price,
            "deviation": 30,
            "magic": self.magic,
            "comment": f"CLOSE_{reason}",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return p.profit
        return None

    def close_all(self, reason: str = ""):
        positions = self.mt5.positions_get(symbol=self.symbol)
        if positions:
            for p in positions:
                if p.magic == self.magic:
                    self.close_position(p.ticket, reason)

    def cancel_pending(self, reason: str = ""):
        orders = self.mt5.orders_get(symbol=self.symbol)
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    request = {
                        "action": self.mt5.TRADE_ACTION_REMOVE,
                        "order": o.ticket,
                    }
                    self.mt5.order_send(request)


# ===================================================================
# Factory
# ===================================================================
def create_gateway(cfg: dict) -> BrokerGateway:
    """Create the appropriate gateway based on config mode."""
    mode = cfg.get("mode", "dry_run")
    if mode == "live":
        return LiveGateway(cfg)
    return DryRunGateway(cfg)
