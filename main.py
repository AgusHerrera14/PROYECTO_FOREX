#!/usr/bin/env python3
"""
main.py - FundedNext EURUSD Trading Bot Orchestrator.

Modes:
  dry_run:  Mac testing. Uses yfinance data + simulated execution.
  backtest: Historical simulation. Runs through all bars, prints report.
  live:     Windows VPS. Connects to MT5 for real execution.

Usage:
  python main.py                  # Uses config.yaml mode
  python main.py --mode backtest  # Override mode
"""
from __future__ import annotations
import argparse
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
import numpy as np
import pandas as pd

from core.indicators import calculate_all, calculate_h4_trend, resample_to_h4, price_to_pips
from core.strategy import get_signal, diag as strategy_diag, reset_diagnostics
from core.risk import calculate_lot_size, validate_sl_tp
from core.compliance import ComplianceEngine, RiskState
from core.news import NewsFilter
from broker.mt5_gateway import create_gateway, DryRunGateway
from broker.data_feed import DataFeed
from ops.logger import TradeLogger
from ops.alerts import TelegramAlerts


# ==================================================================
# LOAD CONFIG
# ==================================================================
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ==================================================================
# SESSION FILTER
# ==================================================================
def is_active_session(cfg: dict) -> bool:
    s = cfg["session"]
    if not s["enabled"]:
        return True
    utc_hour = datetime.now(timezone.utc).hour
    start, end = s["start_hour_utc"], s["end_hour_utc"]
    if start <= end:
        return start <= utc_hour < end
    return utc_hour >= start or utc_hour < end


def is_active_session_backtest(cfg: dict, bar_time: datetime) -> bool:
    """Session filter using bar time instead of wall clock."""
    s = cfg["session"]
    if not s["enabled"]:
        return True
    utc_hour = bar_time.hour
    start, end = s["start_hour_utc"], s["end_hour_utc"]
    if start <= end:
        return start <= utc_hour < end
    return utc_hour >= start or utc_hour < end


# ==================================================================
# TRADE MANAGEMENT (breakeven, trailing)
# ==================================================================
def manage_open_positions(gateway, cfg: dict, atr: float, logger: TradeLogger):
    """Manage breakeven, trailing stop on open positions."""
    tm = cfg["trade_management"]
    positions = gateway.get_positions()

    for pos in positions:
        if pos.sl_pips <= 0:
            continue

        # Current price
        if pos.direction > 0:
            current = gateway.get_bid()
        else:
            current = gateway.get_ask()
        if current <= 0:
            continue

        profit_pips = price_to_pips(
            (current - pos.entry) if pos.direction > 0 else (pos.entry - current)
        )
        r_multiple = profit_pips / pos.sl_pips if pos.sl_pips > 0 else 0

        # --- Breakeven ---
        if tm["breakeven_enabled"] and not pos.be_activated and r_multiple >= tm["breakeven_r"]:
            from core.indicators import pip_size
            ps = pip_size()
            if pos.direction > 0:
                new_sl = pos.entry + ps  # 1 pip above entry
                if new_sl > pos.sl:
                    if gateway.modify_sl(pos.ticket, round(new_sl, 5)):
                        pos.be_activated = True
                        logger.info(f"BREAKEVEN activated ticket {pos.ticket} at {r_multiple:.1f}R")
            else:
                new_sl = pos.entry - ps
                if new_sl < pos.sl:
                    if gateway.modify_sl(pos.ticket, round(new_sl, 5)):
                        pos.be_activated = True
                        logger.info(f"BREAKEVEN activated ticket {pos.ticket} at {r_multiple:.1f}R")

        # --- Trailing stop ---
        if tm["trailing_enabled"] and pos.be_activated and r_multiple >= tm["trailing_start_r"] and atr > 0:
            trail_dist = atr * tm["trailing_atr_mult"]
            if pos.direction > 0:
                new_sl = current - trail_dist
                if new_sl > pos.sl and new_sl > pos.entry:
                    gateway.modify_sl(pos.ticket, round(new_sl, 5))
            else:
                new_sl = current + trail_dist
                if new_sl < pos.sl and new_sl < pos.entry:
                    gateway.modify_sl(pos.ticket, round(new_sl, 5))


# ==================================================================
# SINGLE CYCLE (called every check_interval)
# ==================================================================
def run_cycle(cfg: dict, gateway, data_feed: DataFeed,
              compliance: ComplianceEngine, news: NewsFilter,
              logger: TradeLogger, alerts: TelegramAlerts,
              state: dict):
    """Execute one trading cycle. Returns updated state dict."""

    logger.check_new_day()

    # --- 1. Health check ---
    if not gateway.is_connected():
        if state.get("was_connected", True):
            logger.error("Broker disconnected!")
            alerts.safe_mode("MT5 disconnected")
            state["was_connected"] = False
        return state
    state["was_connected"] = True

    # --- 2. Get data ---
    h1 = data_feed.get_h1(300)
    h4 = data_feed.get_h4(200)

    if h1 is None or h4 is None or len(h1) < 50 or len(h4) < 20:
        logger.debug("Insufficient data")
        return state

    # Calculate indicators
    h1 = calculate_all(h1, cfg)
    h4 = calculate_h4_trend(h4, cfg)

    # Update current price in dry-run gateway
    if isinstance(gateway, DryRunGateway):
        last_close = h1.iloc[-1]["close"]
        gateway.update_price(last_close)

    # Get ATR for trade management
    atr = h1.iloc[-1].get("atr", 0) if "atr" in h1.columns else 0

    # --- 3. Manage open positions (every cycle) ---
    manage_open_positions(gateway, cfg, atr, logger)

    # --- 4. News defensive actions ---
    news.refresh()
    if news.is_blocked():
        if news.should_cancel_pendings():
            gateway.cancel_pending("NEWS_WINDOW")
        # Note: we don't force-close or move to BE every cycle
        # to avoid excessive modifications

    # --- 5. Check for new H1 bar ---
    last_bar_time = h1.index[-1]
    if last_bar_time == state.get("last_bar_time"):
        return state  # No new bar
    state["last_bar_time"] = last_bar_time

    # === NEW BAR PROCESSING ===
    logger.debug(f"New H1 bar: {last_bar_time}")

    # --- 6. Account info ---
    acct = gateway.get_account_info()

    # --- 7. Compliance check ---
    spread = gateway.get_spread_pips()
    block_reason = compliance.rule_check(acct.balance, acct.equity, spread)

    if block_reason:
        if compliance.state == RiskState.KILL_SWITCH:
            gateway.close_all("KILL_SWITCH")
            gateway.cancel_pending("KILL_SWITCH")
            logger.error(f"KILL SWITCH: {block_reason}")
            alerts.rule_breach("KILL_SWITCH", block_reason)
        else:
            logger.debug(f"Blocked: {block_reason}")
        return state

    # --- 8. Session filter ---
    if not is_active_session(cfg):
        logger.debug("Outside trading session")
        return state

    # --- 9. News filter ---
    if news.is_blocked():
        logger.debug(f"News filter: {news.get_status()}")
        return state

    # --- 10. Already have a position? ---
    if gateway.has_position():
        return state

    # --- 11. Generate signal ---
    ask = gateway.get_ask()
    bid = gateway.get_bid()
    signal = get_signal(h1, h4, cfg, current_ask=ask, current_bid=bid)

    if signal is None:
        return state

    # --- 12. Validate SL/TP ---
    if not validate_sl_tp(signal.entry, signal.sl, signal.tp, signal.direction):
        logger.warning(f"Invalid SL/TP: entry={signal.entry}, sl={signal.sl}, tp={signal.tp}")
        return state

    # --- 13. Calculate lot size ---
    risk_pct = compliance.get_risk_percent()
    lots = calculate_lot_size(
        balance=acct.balance,
        risk_pct=risk_pct,
        sl_pips=signal.sl_pips,
        pip_value_per_lot=cfg["risk"]["pip_value_per_lot"],
    )
    if lots <= 0:
        logger.warning(f"Lot size = 0. SL pips: {signal.sl_pips}")
        return state

    # --- 14. Final spread check ---
    spread = gateway.get_spread_pips()
    if spread > cfg["risk"]["max_spread_pips"]:
        logger.debug(f"Spread too high: {spread:.1f}")
        return state

    # --- 15. Execute trade ---
    direction_str = "BUY" if signal.direction > 0 else "SELL"
    result = gateway.open_trade(signal.direction, lots, signal.sl, signal.tp,
                                comment="TrendPullback")

    if result.success:
        compliance.on_trade_opened()
        dd_pct = compliance.get_total_dd_pct(acct.equity)
        news_status = news.get_status()

        logger.log_trade_open(
            ticket=result.ticket, strategy="TrendPullback",
            direction=direction_str, entry=result.price,
            sl=signal.sl, tp=signal.tp, lots=lots,
            risk_pct=risk_pct, spread=spread,
            news_status=news_status, reason=signal.reason,
            equity=acct.equity, balance=acct.balance, dd_pct=dd_pct,
        )
        logger.info(f"TRADE OPENED: {direction_str} {lots:.2f} lots @ {result.price:.5f} | "
                     f"SL: {signal.sl_pips:.1f} pips | TP: {signal.tp_pips:.1f} pips | "
                     f"Spread: {spread:.1f}")
        alerts.trade_opened(direction_str, lots, result.price,
                            signal.sl, signal.tp, signal.sl_pips, signal.reason)
    else:
        logger.error(f"Trade failed: {result.error}")

    return state


# ==================================================================
# REAL-TIME LOOP (dry_run / live)
# ==================================================================
def run_realtime(cfg: dict):
    """Main loop for dry_run and live modes."""
    logger = TradeLogger(cfg)
    alerts = TelegramAlerts(cfg)
    news = NewsFilter(cfg)
    compliance = ComplianceEngine(cfg)
    gateway = create_gateway(cfg)
    data_feed = DataFeed(cfg, gateway)

    mode = cfg["mode"]
    interval = cfg["check_interval_seconds"]

    logger.info(f"========== BOT STARTING ({mode.upper()}) ==========")

    if not gateway.connect():
        logger.error("Cannot connect to broker. Exiting.")
        alerts.safe_mode("Cannot connect to broker")
        return

    acct = gateway.get_account_info()
    logger.info(f"Balance: ${acct.balance:.2f} | Mode: {mode}")
    alerts.bot_started(mode, acct.balance)

    state = {"last_bar_time": None, "was_connected": True, "cycles": 0}

    try:
        while True:
            try:
                state = run_cycle(cfg, gateway, data_feed, compliance, news,
                                  logger, alerts, state)
                state["cycles"] = state.get("cycles", 0) + 1

                # Hourly heartbeat
                if state["cycles"] % (3600 // max(interval, 1)) == 0:
                    acct = gateway.get_account_info()
                    summary = compliance.get_state_summary(acct.balance, acct.equity)
                    logger.info(f"HEARTBEAT | Bal: ${acct.balance:.2f} | "
                                f"Eq: ${acct.equity:.2f} | DD: {summary['total_dd_pct']:.2f}% | "
                                f"State: {summary['state']}")
                    alerts.heartbeat(acct.balance, acct.equity, summary["total_dd_pct"],
                                     summary["trades_today"], summary["state"])

            except Exception as e:
                logger.error(f"Cycle error: {e}\n{traceback.format_exc()}")
                alerts.send(f"⚠️ Error: {e}")

            time.sleep(interval)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
        alerts.bot_stopped("User interrupt")
    finally:
        gateway.close_all("BOT_SHUTDOWN")
        gateway.disconnect()
        logger.close()


# ==================================================================
# BACKTEST MODE
# ==================================================================
def run_backtest(cfg: dict):
    """Historical backtest. Iterates through all H1 bars."""
    bt = cfg["backtest"]
    logger = TradeLogger(cfg)
    compliance = ComplianceEngine(cfg)
    gateway = DryRunGateway(cfg)
    gateway.connect()
    data_feed = DataFeed(cfg, gateway)

    logger.info("========== BACKTEST STARTING ==========")
    logger.info(f"Period: {bt['start_date']} to {bt['end_date']}")

    # Reset strategy diagnostics
    reset_diagnostics()

    # Load all data
    h1_all = data_feed.get_historical("1h", bt["start_date"], bt["end_date"])
    if h1_all is None or len(h1_all) < 300:
        logger.error("Not enough historical data for backtest")
        return

    logger.info(f"Loaded {len(h1_all)} H1 bars")
    logger.info(f"Date range: {h1_all.index[0]} → {h1_all.index[-1]}")

    # Calculate all indicators on full dataset
    h1_all = calculate_all(h1_all, cfg)
    h4_all = resample_to_h4(h1_all)
    h4_all = calculate_h4_trend(h4_all, cfg)

    logger.info(f"H4 bars: {len(h4_all)}")

    # Warmup: skip first 250 bars
    warmup = 250
    trades_total = 0
    trades_won = 0

    # Diagnostic counters
    bt_diag = {
        "bars_processed": 0,
        "h4_insufficient": 0,
        "compliance_blocked": 0,
        "position_open": 0,
        "session_blocked": 0,
        "no_signal": 0,
        "sl_invalid": 0,
        "lot_zero": 0,
        "trades_opened": 0,
        "kill_switch": False,
    }

    for i in range(warmup, len(h1_all)):
        bar = h1_all.iloc[i]
        bar_time = h1_all.index[i]
        bt_diag["bars_processed"] += 1

        # Convert pandas Timestamp to datetime for compliance
        bar_dt = bar_time.to_pydatetime() if hasattr(bar_time, 'to_pydatetime') else bar_time

        # Advance compliance calendar BEFORE processing anything
        compliance.advance_time(bar_dt, gateway.balance)

        # Update price in gateway
        gateway.update_price(bar["close"])

        # Check SL/TP hits on this bar (with breakeven/trailing management)
        bar_atr = bar.get("atr", 0) if "atr" in h1_all.columns else 0
        gateway.check_sl_tp({
            "high": bar["high"],
            "low": bar["low"],
            "open": bar["open"],
            "close": bar["close"],
            "atr": bar_atr,
        }, cfg=cfg)

        # Process closed trades
        while len(gateway.trade_history) > trades_total:
            trade = gateway.trade_history[trades_total]
            pnl = trade["pnl"]
            compliance.on_trade_closed(pnl, gateway.balance)
            trades_total += 1
            if pnl > 0:
                trades_won += 1

        # H1 window + H4 window
        h1_window = h1_all.iloc[max(0, i - 300):i + 1]
        # Find matching H4 bars
        h4_window = h4_all[h4_all.index <= bar_time].tail(200)

        if len(h4_window) < 20:
            bt_diag["h4_insufficient"] += 1
            continue

        # Session filter (use bar time, not wall clock)
        if not is_active_session_backtest(cfg, bar_dt):
            bt_diag["session_blocked"] += 1
            continue

        # Compliance check
        acct = gateway.get_account_info()
        block = compliance.rule_check(acct.balance, acct.equity, gateway.get_spread_pips())
        if block:
            if compliance.state == RiskState.KILL_SWITCH:
                gateway.close_all("KILL_SWITCH")
                bt_diag["kill_switch"] = True
                break
            bt_diag["compliance_blocked"] += 1
            continue

        # Skip if position open
        if gateway.has_position():
            bt_diag["position_open"] += 1
            continue

        # Generate signal
        signal = get_signal(h1_window, h4_window, cfg)
        if signal is None:
            bt_diag["no_signal"] += 1
            continue

        # Validate
        if not validate_sl_tp(signal.entry, signal.sl, signal.tp, signal.direction):
            bt_diag["sl_invalid"] += 1
            continue

        # Size
        risk_pct = compliance.get_risk_percent()
        lots = calculate_lot_size(
            balance=acct.balance, risk_pct=risk_pct, sl_pips=signal.sl_pips,
            pip_value_per_lot=cfg["risk"]["pip_value_per_lot"],
        )
        if lots <= 0:
            bt_diag["lot_zero"] += 1
            continue

        # Execute
        gateway.open_trade(signal.direction, lots, signal.sl, signal.tp, "TrendPullback")
        compliance.on_trade_opened()
        bt_diag["trades_opened"] += 1

    # Close any remaining position
    gateway.close_all("BACKTEST_END")
    while len(gateway.trade_history) > trades_total:
        trade = gateway.trade_history[trades_total]
        compliance.on_trade_closed(trade["pnl"], gateway.balance)
        trades_total += 1
        if trade["pnl"] > 0:
            trades_won += 1

    # === REPORT ===
    print_backtest_report(gateway, trades_total, trades_won, cfg)
    print_backtest_diagnostics(bt_diag, strategy_diag)
    logger.close()


def print_backtest_report(gateway: DryRunGateway, total: int, wins: int, cfg: dict):
    """Print comprehensive backtest results."""
    init_bal = cfg["compliance"]["initial_balance"]
    final_bal = gateway.balance

    trades = gateway.trade_history
    if not trades:
        print("\n⚠️  NO TRADES GENERATED. Check strategy parameters.\n")
        return

    pnls = [t["pnl"] for t in trades]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]

    win_rate = wins / total * 100 if total > 0 else 0
    avg_win = np.mean(win_pnls) if win_pnls else 0
    avg_loss = np.mean(loss_pnls) if loss_pnls else 0
    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    expectancy = np.mean(pnls) if pnls else 0

    # Max drawdown
    equity_curve = np.cumsum(pnls) + init_bal
    peak = np.maximum.accumulate(equity_curve)
    drawdown = peak - equity_curve
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
    max_dd_pct = max_dd / init_bal * 100

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for p in pnls:
        if p <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    # Sharpe (approximate)
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252)
    else:
        sharpe = 0

    net_pnl = final_bal - init_bal
    net_pct = net_pnl / init_bal * 100

    # Trade duration stats
    buy_count = sum(1 for t in trades if t["direction"] == "BUY")
    sell_count = sum(1 for t in trades if t["direction"] == "SELL")
    sl_hits = sum(1 for t in trades if t["reason"] == "SL_HIT")
    tp_hits = sum(1 for t in trades if t["reason"] == "TP_HIT")
    be_hits = sum(1 for t in trades if t["reason"] == "BE_HIT")
    trail_hits = sum(1 for t in trades if t["reason"] == "TRAIL_HIT")
    partial_hits = sum(1 for t in trades if t["reason"] == "PARTIAL_1R")
    other = total - sl_hits - tp_hits - be_hits - trail_hits - partial_hits

    print("\n" + "=" * 60)
    print("         BACKTEST RESULTS - FundedNext EURUSD")
    print("=" * 60)
    print(f"  Period:           {cfg['backtest']['start_date']} → {cfg['backtest']['end_date']}")
    print(f"  Initial Balance:  ${init_bal:,.2f}")
    print(f"  Final Balance:    ${final_bal:,.2f}")
    print(f"  Net P&L:          ${net_pnl:,.2f} ({net_pct:+.1f}%)")
    print(f"  Spread sim:       {cfg['backtest']['spread_pips']} pips")
    print(f"  Slippage sim:     {cfg['backtest']['slippage_pips']} pips")
    print("-" * 60)
    print(f"  Total Trades:     {total}")
    print(f"  Buys / Sells:     {buy_count} / {sell_count}")
    print(f"  Wins:             {wins} ({win_rate:.1f}%)")
    print(f"  Losses:           {total - wins}")
    print(f"  SL hits / TP hits / BE / Trail / Partial / Other: {sl_hits} / {tp_hits} / {be_hits} / {trail_hits} / {partial_hits} / {other}")
    print(f"  Avg Win:          ${avg_win:.2f}")
    print(f"  Avg Loss:         ${avg_loss:.2f}")
    print(f"  Profit Factor:    {profit_factor:.2f}")
    print(f"  Expectancy:       ${expectancy:.2f}/trade")
    print(f"  Sharpe (approx):  {sharpe:.2f}")
    print("-" * 60)
    print(f"  Max Drawdown:     ${max_dd:.2f} ({max_dd_pct:.2f}%)")
    print(f"  Max Consec Loss:  {max_consec}")
    print("-" * 60)

    # GO / NO-GO
    go = True
    checks = []
    if profit_factor >= 1.20:
        checks.append(f"  ✅ Profit Factor {profit_factor:.2f} >= 1.20")
    else:
        checks.append(f"  ❌ Profit Factor {profit_factor:.2f} < 1.20")
        go = False

    if max_dd_pct < 8.0:
        checks.append(f"  ✅ Max DD {max_dd_pct:.2f}% < 8%")
    else:
        checks.append(f"  ❌ Max DD {max_dd_pct:.2f}% >= 8%")
        go = False

    if total >= 50:
        checks.append(f"  ✅ Trades {total} >= 50")
    else:
        checks.append(f"  ❌ Trades {total} < 50")
        go = False

    if expectancy > 0:
        checks.append(f"  ✅ Expectancy ${expectancy:.2f} > $0")
    else:
        checks.append(f"  ❌ Expectancy ${expectancy:.2f} <= $0")
        go = False

    if win_rate >= 40:
        checks.append(f"  ✅ Win Rate {win_rate:.1f}% >= 40%")
    else:
        checks.append(f"  ❌ Win Rate {win_rate:.1f}% < 40%")
        go = False

    print("\n  GO / NO-GO CHECKS:")
    for c in checks:
        print(c)

    verdict = "🟢 GO → Proceed to demo testing" if go else "🔴 NO-GO → Review strategy parameters"
    print(f"\n  VERDICT: {verdict}")
    print("=" * 60 + "\n")


def print_backtest_diagnostics(bt_diag: dict, strat_diag: dict):
    """Print diagnostic breakdown of signal pipeline."""
    print("=" * 60)
    print("         BACKTEST DIAGNOSTICS")
    print("=" * 60)
    print(f"  Bars processed:      {bt_diag['bars_processed']}")
    print(f"  H4 insufficient:     {bt_diag['h4_insufficient']}")
    print(f"  Session blocked:     {bt_diag['session_blocked']}")
    print(f"  Compliance blocked:  {bt_diag['compliance_blocked']}")
    print(f"  Position open:       {bt_diag['position_open']}")
    print(f"  Kill switch:         {bt_diag['kill_switch']}")
    print("-" * 60)
    print("  STRATEGY FILTER BREAKDOWN:")
    print(f"    Signal checks:     {strat_diag['total_checks']}")
    print(f"    No H4 indicators:  {strat_diag.get('no_h4_indicators', 0)}")
    print(f"    No H4 trend:       {strat_diag['no_h4_trend']}")
    print(f"    No H4 slope:       {strat_diag.get('no_h4_slope', 0)}")
    print(f"    No EMA21 slope:    {strat_diag.get('no_ema21_slope', 0)}")
    print(f"    No EMA pullback:   {strat_diag['no_pullback']}")
    print(f"    RSI out of range:  {strat_diag['no_rsi']}")
    print(f"    Wrong candle:      {strat_diag['no_candle']}")
    print(f"    SL invalid:        {strat_diag['no_sl_valid']}")
    print(f"    Signals generated: {strat_diag['signals_generated']}")
    print(f"      Breakout sigs:  {strat_diag.get('breakout_signals', 0)}")
    print(f"      Pullback sigs:  {strat_diag.get('pullback_signals', 0)}")
    # London Breakout specific
    if strat_diag.get('london_wrong_hour', 0) > 0 or strat_diag.get('london_no_range', 0) > 0:
        print(f"  LONDON BREAKOUT DETAILS:")
        print(f"    Wrong hour:       {strat_diag.get('london_wrong_hour', 0)}")
        print(f"    No valid range:   {strat_diag.get('london_no_range', 0)}")
        print(f"    No breakout:      {strat_diag.get('london_no_break', 0)}")
        print(f"    Trend filtered:   {strat_diag.get('london_trend_filter', 0)}")
    print("-" * 60)
    print(f"  SL/TP invalid:       {bt_diag['sl_invalid']}")
    print(f"  Lot size zero:       {bt_diag['lot_zero']}")
    print(f"  Trades opened:       {bt_diag['trades_opened']}")
    print("=" * 60 + "\n")


# ==================================================================
# ENTRY POINT
# ==================================================================
def main():
    parser = argparse.ArgumentParser(description="FundedNext EURUSD Trading Bot")
    parser.add_argument("--mode", choices=["dry_run", "backtest", "live"],
                        help="Override mode from config.yaml")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode

    mode = cfg["mode"]
    print(f"\n🤖 FundedNext EURUSD Bot v2.0 | Mode: {mode.upper()}\n")

    if mode == "backtest":
        run_backtest(cfg)
    else:
        run_realtime(cfg)


if __name__ == "__main__":
    main()
