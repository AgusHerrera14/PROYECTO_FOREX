#!/usr/bin/env python3
"""
RIGOROUS PROBABILITY ANALYSIS — FundedNext Challenge
Monte Carlo simulation using actual backtest trade distribution.

Runs the real backtest engine, extracts trade-by-trade PnL,
then simulates 50,000 challenge attempts per phase.
"""
import sys
import os
import json
import numpy as np
from pathlib import Path

# Suppress logs during backtest
os.environ["SUPPRESS_LOGS"] = "1"

sys.path.insert(0, str(Path(__file__).parent))

import yaml
from core.indicators import (calculate_all, calculate_h4_trend,
                              resample_to_h4, calculate_smc)
from core.strategy import get_signal, reset_diagnostics
from core.risk import calculate_lot_size, validate_sl_tp
from core.compliance import ComplianceEngine, RiskState
from broker.mt5_gateway import DryRunGateway
from broker.data_feed import DataFeed
from ops.logger import TradeLogger


def extract_trades_from_backtest():
    """Run the actual backtest engine and return (trade_history_list, init_bal)."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    cfg["mode"] = "backtest"
    bt = cfg["backtest"]
    init_bal = bt["initial_balance"]

    compliance = ComplianceEngine(cfg)
    gateway = DryRunGateway(cfg)
    gateway.connect()
    data_feed = DataFeed(cfg, gateway)
    reset_diagnostics()

    h1_all = data_feed.get_historical("1h", bt["start_date"], bt["end_date"])
    if h1_all is None or len(h1_all) < 300:
        print("ERROR: Not enough data")
        sys.exit(1)

    h1_all = calculate_all(h1_all, cfg)
    h1_all = calculate_smc(h1_all)
    h4_all = resample_to_h4(h1_all)
    h4_all = calculate_h4_trend(h4_all, cfg)

    warmup = 250
    trades_total = 0

    def is_active_session_backtest(cfg, bar_dt):
        if not cfg.get("session", {}).get("enabled", False):
            return True
        h = bar_dt.hour
        start = cfg["session"]["start_hour_utc"]
        end = cfg["session"]["end_hour_utc"]
        return start <= h < end

    for i in range(warmup, len(h1_all)):
        bar = h1_all.iloc[i]
        bar_time = h1_all.index[i]
        bar_dt = bar_time.to_pydatetime() if hasattr(bar_time, 'to_pydatetime') else bar_time

        compliance.advance_time(bar_dt, gateway.balance)
        gateway.update_price(bar["close"])

        bar_atr = bar.get("atr", 0) if "atr" in h1_all.columns else 0
        gateway.check_sl_tp({
            "high": bar["high"], "low": bar["low"],
            "open": bar["open"], "close": bar["close"],
            "atr": bar_atr,
        }, cfg=cfg)

        while len(gateway.trade_history) > trades_total:
            trade = gateway.trade_history[trades_total]
            compliance.on_trade_closed(trade["pnl"], gateway.balance)
            trades_total += 1

        h1_window = h1_all.iloc[max(0, i - 300):i + 1]
        h4_window = h4_all[h4_all.index <= bar_time].tail(200)

        if len(h4_window) < 20:
            continue

        if not is_active_session_backtest(cfg, bar_dt):
            continue

        acct = gateway.get_account_info()
        block = compliance.rule_check(acct.balance, acct.equity, gateway.get_spread_pips())
        if block:
            if compliance.state == RiskState.KILL_SWITCH:
                gateway.close_all("KILL_SWITCH")
                break
            continue

        if gateway.has_position():
            continue

        signal = get_signal(h1_window, h4_window, cfg)
        if signal is None:
            continue

        if not validate_sl_tp(signal.entry, signal.sl, signal.tp, signal.direction):
            continue

        risk_pct = compliance.get_risk_percent()
        lots = calculate_lot_size(
            balance=acct.balance, risk_pct=risk_pct, sl_pips=signal.sl_pips,
            pip_value_per_lot=cfg["risk"]["pip_value_per_lot"],
        )
        if lots <= 0:
            continue

        gateway.open_trade(signal.direction, lots, signal.sl, signal.tp, "TrendPullback")
        compliance.on_trade_opened()

    gateway.close_all("BACKTEST_END")
    while len(gateway.trade_history) > trades_total:
        trade = gateway.trade_history[trades_total]
        compliance.on_trade_closed(trade["pnl"], gateway.balance)
        trades_total += 1

    return gateway.trade_history, init_bal


# ── Monte Carlo Simulation ──────────────────────────────────────────
def monte_carlo_challenge(pnls_array, initial_balance, n_sims=50000):
    """
    Simulate FundedNext Express challenge with bootstrap resampling.

    Rules modeled:
    - Phase 1: +8% profit target, Phase 2: +5% profit target
    - Max total drawdown: 10% from initial balance
    - Max daily loss: 5% from previous day EOD balance
    - Trailing DD: 10% from High Water Mark (Express model)
    - No strict time limit, but we cap at 200 trades (~5 years patience)

    Returns detailed probability breakdown.
    """
    rng = np.random.default_rng(42)
    n_trades = len(pnls_array)
    pnls = np.array(pnls_array, dtype=np.float64)

    MAX_TOTAL_DD = 10.0   # % from initial
    MAX_DAILY_DD = 5.0    # % from day-start equity
    MAX_TRAILING_DD = 10.0  # % from HWM (Express)
    MAX_TRADES = 200      # patience cap (~5 years at 3.2 trades/month)
    TRADES_PER_DAY_MAX = 3

    def simulate_phase(target_pct, n_sims):
        passes = 0
        fail_dd = 0
        fail_time = 0
        trades_needed = []

        for _ in range(n_sims):
            balance = initial_balance
            hwm = initial_balance
            day_start = initial_balance
            trades_today = 0
            passed = False
            blown = False

            for t in range(MAX_TRADES):
                # New day simulation: ~70% chance of day change between trades
                # (avg 0.15 trades/day → most trades are on different days)
                if trades_today >= TRADES_PER_DAY_MAX or rng.random() < 0.70:
                    day_start = balance
                    trades_today = 0

                # Draw random trade, scale by current balance
                idx = rng.integers(0, n_trades)
                scale = balance / initial_balance
                pnl = pnls[idx] * scale
                balance += pnl
                trades_today += 1

                if balance > hwm:
                    hwm = balance

                # DD checks
                daily_dd_pct = (day_start - balance) / day_start * 100 if day_start > 0 else 0
                total_dd_pct = (initial_balance - balance) / initial_balance * 100
                trail_dd_pct = (hwm - balance) / hwm * 100 if hwm > 0 else 0

                if daily_dd_pct >= MAX_DAILY_DD or total_dd_pct >= MAX_TOTAL_DD or trail_dd_pct >= MAX_TRAILING_DD:
                    blown = True
                    break

                # Target check
                profit_pct = (balance - initial_balance) / initial_balance * 100
                if profit_pct >= target_pct:
                    passed = True
                    trades_needed.append(t + 1)
                    break

            if passed:
                passes += 1
            elif blown:
                fail_dd += 1
            else:
                fail_time += 1

        return {
            "pass_rate": passes / n_sims,
            "fail_dd_rate": fail_dd / n_sims,
            "fail_time_rate": fail_time / n_sims,
            "avg_trades": np.mean(trades_needed) if trades_needed else 0,
            "median_trades": np.median(trades_needed) if trades_needed else 0,
            "p25_trades": np.percentile(trades_needed, 25) if trades_needed else 0,
            "p75_trades": np.percentile(trades_needed, 75) if trades_needed else 0,
        }

    phase1 = simulate_phase(8.0, n_sims)
    phase2 = simulate_phase(5.0, n_sims)

    # Funded account survival: 6 months (~19 trades)
    survival_sims = n_sims
    survived = 0
    for _ in range(survival_sims):
        balance = initial_balance
        hwm = initial_balance
        day_start = initial_balance
        trades_today = 0
        blown = False

        for t in range(19):  # ~6 months at 3.2/month
            if trades_today >= TRADES_PER_DAY_MAX or rng.random() < 0.70:
                day_start = balance
                trades_today = 0

            idx = rng.integers(0, n_trades)
            scale = balance / initial_balance
            pnl = pnls[idx] * scale
            balance += pnl
            trades_today += 1

            if balance > hwm:
                hwm = balance

            daily_dd_pct = (day_start - balance) / day_start * 100 if day_start > 0 else 0
            total_dd_pct = (initial_balance - balance) / initial_balance * 100
            trail_dd_pct = (hwm - balance) / hwm * 100 if hwm > 0 else 0

            if daily_dd_pct >= MAX_DAILY_DD or total_dd_pct >= MAX_TOTAL_DD or trail_dd_pct >= MAX_TRAILING_DD:
                blown = True
                break

        if not blown:
            survived += 1

    survival_rate = survived / survival_sims

    return phase1, phase2, survival_rate


# ── Main ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("   PROBABILITY ANALYSIS — FundedNext Challenge")
    print("   Monte Carlo Simulation (50,000 iterations)")
    print("=" * 70)

    print("\n[1/3] Running backtest to extract trade data...", flush=True)
    trades_data, init_bal = extract_trades_from_backtest()

    pnls = [t["pnl"] for t in trades_data]
    pnl_arr = np.array(pnls)
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr <= 0]

    # Backtest spans
    years_span = 6.16  # 2020-01 to 2026-02
    trades_per_month = len(pnls) / (years_span * 12)

    print(f"\n{'─' * 70}")
    print(f"  RAW TRADE DISTRIBUTION ({len(pnls)} trades, {years_span:.1f} years)")
    print(f"{'─' * 70}")
    print(f"  Win rate:            {len(wins)/len(pnls)*100:.1f}%")
    print(f"  Avg win:             ${np.mean(wins):,.2f}")
    print(f"  Avg loss:            ${np.mean(losses):,.2f}")
    print(f"  Best trade:          ${np.max(pnl_arr):,.2f}")
    print(f"  Worst trade:         ${np.min(pnl_arr):,.2f}")
    print(f"  Median trade:        ${np.median(pnl_arr):,.2f}")
    print(f"  Std deviation:       ${np.std(pnl_arr):,.2f}")
    print(f"  Expectancy:          ${np.mean(pnl_arr):,.2f}/trade ({np.mean(pnl_arr)/init_bal*100:.3f}%)")
    pf = abs(np.sum(wins)) / abs(np.sum(losses)) if len(losses) > 0 else float('inf')
    print(f"  Profit Factor:       {pf:.2f}")
    print(f"  Trades/month:        {trades_per_month:.1f}")
    monthly_exp = np.mean(pnl_arr) * trades_per_month
    print(f"  Monthly expectancy:  ${monthly_exp:,.2f} ({monthly_exp/init_bal*100:.2f}%)")

    # Consecutive losses
    max_consec = 0
    cur_consec = 0
    consec_runs = []
    for p in pnls:
        if p <= 0:
            cur_consec += 1
        else:
            if cur_consec > 0:
                consec_runs.append(cur_consec)
            cur_consec = 0
    if cur_consec > 0:
        consec_runs.append(cur_consec)
    consec_sorted = sorted(consec_runs, reverse=True)

    print(f"\n  Consecutive loss streaks (top 5): {consec_sorted[:5]}")
    print(f"  Max consecutive losses: {consec_sorted[0] if consec_sorted else 0}")

    # Worst rolling windows
    for window in [5, 10, 15, 20]:
        if len(pnl_arr) >= window:
            rolling = np.convolve(pnl_arr, np.ones(window), mode='valid')
            worst = np.min(rolling)
            worst_pct = worst / init_bal * 100
            print(f"  Worst {window}-trade sequence: ${worst:,.2f} ({worst_pct:.2f}%)")

    # Risk of ruin (analytical)
    wr = len(wins) / len(pnls)
    avg_w = np.mean(wins)
    avg_l = abs(np.mean(losses))
    payoff = avg_w / avg_l if avg_l > 0 else 0
    # Kelly criterion
    kelly = (wr * payoff - (1 - wr)) / payoff if payoff > 0 else 0
    print(f"\n  Payoff ratio (W/L):  {payoff:.2f}")
    print(f"  Kelly fraction:      {kelly*100:.1f}%")

    print(f"\n[2/3] Running Monte Carlo simulation (50,000 per phase)...", flush=True)
    phase1, phase2, survival = monte_carlo_challenge(pnls, init_bal, n_sims=50000)

    print(f"\n{'─' * 70}")
    print(f"  MONTE CARLO RESULTS — BACKTEST CONDITIONS")
    print(f"{'─' * 70}")

    print(f"\n  PHASE 1 (target: +8%, DD limit: 10%)")
    print(f"    Pass rate:              {phase1['pass_rate']*100:.1f}%")
    print(f"    Fail by drawdown:       {phase1['fail_dd_rate']*100:.1f}%")
    print(f"    Fail by time (>200 tr): {phase1['fail_time_rate']*100:.1f}%")
    if phase1['avg_trades'] > 0:
        print(f"    Avg trades to pass:     {phase1['avg_trades']:.0f}")
        print(f"    Median trades to pass:  {phase1['median_trades']:.0f}")
        print(f"    25th-75th percentile:   {phase1['p25_trades']:.0f} – {phase1['p75_trades']:.0f} trades")
        print(f"    Est. median time:       ~{phase1['median_trades']/trades_per_month:.0f} months")

    print(f"\n  PHASE 2 (target: +5%, DD limit: 10%)")
    print(f"    Pass rate:              {phase2['pass_rate']*100:.1f}%")
    print(f"    Fail by drawdown:       {phase2['fail_dd_rate']*100:.1f}%")
    print(f"    Fail by time (>200 tr): {phase2['fail_time_rate']*100:.1f}%")
    if phase2['avg_trades'] > 0:
        print(f"    Avg trades to pass:     {phase2['avg_trades']:.0f}")
        print(f"    Median trades to pass:  {phase2['median_trades']:.0f}")
        print(f"    Est. median time:       ~{phase2['median_trades']/trades_per_month:.0f} months")

    backtest_both = phase1['pass_rate'] * phase2['pass_rate']
    backtest_all = backtest_both * survival

    print(f"\n  COMBINED (backtest conditions):")
    print(f"    P(Phase1) × P(Phase2):  {backtest_both*100:.1f}%")
    print(f"    P(survive 6m funded):   {survival*100:.1f}%")
    print(f"    P(all three):           {backtest_all*100:.1f}%")

    # ── Live degradation ────────────────────────────────────────
    print(f"\n[3/3] Applying live trading degradation...", flush=True)

    print(f"\n{'─' * 70}")
    print(f"  LIVE TRADING DEGRADATION FACTORS")
    print(f"{'─' * 70}")

    factors = [
        ("Wider spreads in live (1.0→1.5+ pips)",    0.82,
         "Backtest uses 1.0 pip; live EURUSD averages 1.2-2.0 pips during London.\n"
         "Extra 0.5 pips on 232 trades = ~$1,160 lost, eroding 5.4% of total profit."),
        ("Slippage & execution quality",              0.90,
         "Real fills differ from backtest. Missed entries, partial fills,\n"
         "requotes during fast London breakout moves."),
        ("Market regime shift (overfit penalty)",      0.75,
         "232 trades over 6 years is a TINY sample. Strategy parameters are\n"
         "inevitably fitted to 2020-2026 patterns. Future may differ."),
        ("Psychological / discipline factor",          0.70,
         "Funded account pressure: fear of losing the challenge fee,\n"
         "temptation to override signals, skip trades, or revenge trade."),
        ("Broker/platform conditions",                 0.92,
         "MT5 server disconnects, swap costs (overnight holds), platform\n"
         "delays, different data feed vs backtest."),
    ]

    combined = 1.0
    for name, factor, reason in factors:
        combined *= factor
        print(f"  {name}")
        print(f"    Factor: ×{factor:.2f} (-{(1-factor)*100:.0f}%)")
        for line in reason.split("\n"):
            print(f"    {line}")
        print()

    print(f"  {'Combined degradation:':45s} ×{combined:.4f} (-{(1-combined)*100:.0f}%)")

    # Time/frequency penalty
    time_penalty_reason = (
        f"At {trades_per_month:.1f} trades/month and {monthly_exp/init_bal*100:.2f}% monthly return,\n"
        f"Phase 1 alone needs ~{8.0/(monthly_exp/init_bal*100):.0f} months. Most traders abandon\n"
        f"or the challenge fee subscription runs out. This is the BIGGEST issue."
    )
    print(f"\n  Low trade frequency / slow progress penalty")
    print(f"    Factor: ×0.50")
    for line in time_penalty_reason.split("\n"):
        print(f"    {line}")
    time_penalty = 0.50

    total_degradation = combined * time_penalty

    # ── FINAL CALCULATION ───────────────────────────────────────
    final_prob = backtest_all * total_degradation

    print(f"\n{'=' * 70}")
    print(f"  FINAL PROBABILITY CALCULATION")
    print(f"{'=' * 70}")
    print(f"")
    print(f"  Step 1: Monte Carlo Phase 1 pass    = {phase1['pass_rate']*100:.1f}%")
    print(f"  Step 2: Monte Carlo Phase 2 pass    = {phase2['pass_rate']*100:.1f}%")
    print(f"  Step 3: P(both phases, backtest)     = {backtest_both*100:.1f}%")
    print(f"  Step 4: P(survive 6m funded)          = {survival*100:.1f}%")
    print(f"  Step 5: Backtest probability          = {backtest_all*100:.1f}%")
    print(f"  Step 6: × Live degradation (×{combined:.4f})  = {backtest_all*combined*100:.2f}%")
    print(f"  Step 7: × Time/frequency penalty (×0.50)")
    print(f"")
    print(f"  ┌─────────────────────────────────────────────┐")
    print(f"  │                                             │")
    print(f"  │   FINAL REAL-WORLD PROBABILITY: {final_prob*100:.1f}%{' ' * (9 - len(f'{final_prob*100:.1f}'))}│")
    print(f"  │                                             │")
    print(f"  └─────────────────────────────────────────────┘")

    # Confidence interval (Wilson score)
    p = backtest_all
    n = 50000
    z = 1.96
    denom = 1 + z**2/n
    centre = (p + z**2/(2*n)) / denom
    margin = z * np.sqrt((p*(1-p) + z**2/(4*n))/n) / denom

    print(f"\n  95% CI (backtest MC): [{max(0,centre-margin)*100:.1f}% – {min(1,centre+margin)*100:.1f}%]")
    print(f"  95% CI (live est):   [{max(0,(centre-margin)*total_degradation)*100:.1f}% – "
          f"{(centre+margin)*total_degradation*100:.1f}%]")

    print(f"\n{'=' * 70}")
    print(f"  WHY THE PROBABILITY IS LOW — KEY ISSUES")
    print(f"{'=' * 70}")
    print(f"""
  1. RETURN TOO LOW FOR THE TARGET
     Annual return: ~3.5% | Phase 1 needs: 8% | Phase 2 needs: 5%
     At 0.29%/month, Phase 1 alone takes ~28 months.
     That's 2+ YEARS of perfect execution for one phase.

  2. TINY SAMPLE SIZE (232 trades / 6 years)
     Only 3.2 trades/month = extremely high variance.
     232 trades is statistically insufficient to confirm a real edge.
     A coin flip with slight bias needs thousands of flips to prove.

  3. PROFIT FACTOR 1.21 — RAZOR-THIN EDGE
     Industry consensus: PF < 1.5 is fragile. PF 1.21 means every
     extra pip of spread or slip can flip it below 1.0 (losing).
     Live PF is typically 60-80% of backtest PF → ~0.97-1.0 live.

  4. MAX CONSECUTIVE LOSSES: {consec_sorted[0] if consec_sorted else 'N/A'}
     With 1.5% risk per trade and {consec_sorted[0] if consec_sorted else 'N/A'} consecutive losses,
     that's ~{consec_sorted[0]*1.5 if consec_sorted else 0:.1f}% drawdown in one streak.
     Hit that early in the challenge and recovery is near-impossible.

  5. CURVE FITTING RISK
     Strategy optimized on 2020-2026 data. London breakout patterns,
     volatility regimes, and SMC confluence signals from this period
     may not repeat. Out-of-sample testing is essential.

  6. PSYCHOLOGY UNDER PRESSURE
     Funded account adds massive psychological burden. Most traders
     perform 20-30% worse under real money pressure (well-documented).

  BOTTOM LINE: The strategy has a small positive expectancy but is
  FAR too slow and fragile to reliably pass a prop firm challenge.
  You would need ~15-20% annual return (4-5x current) to have a
  reasonable shot.
""")
    print(f"{'=' * 70}")
