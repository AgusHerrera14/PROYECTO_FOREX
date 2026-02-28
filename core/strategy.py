"""
core/strategy.py - Trading strategy module.

Supports two strategies (configured via london_breakout flag):

1. London Breakout + SMC (primary):
   - Calculates Asian session range (00:00-06:00 UTC)
   - Enters on breakout during London session (07:00-10:00 UTC)
   - SMC confluence: FVG, Order Blocks, Liquidity Sweeps
   - SL at opposite end of Asian range (or Order Block)
   - TP = SL * RR ratio
   - EMA200 trend bias filter + SMC filters

2. Momentum Breakout (fallback):
   - H1 EMA200 trend + Donchian breakout / EMA21 pullback
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np
from core.indicators import (price_to_pips, pip_size, has_recent_fvg,
                              has_recent_liquidity_sweep, get_nearest_ob_sl)


@dataclass
class Signal:
    direction: int = 0
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    sl_pips: float = 0.0
    tp_pips: float = 0.0
    reason: str = ""


diag = {
    "total_checks": 0,
    "no_h4_indicators": 0,
    "no_h4_trend": 0,
    "no_h4_slope": 0,
    "no_ema21_slope": 0,
    "no_pullback": 0,
    "no_rsi": 0,
    "no_candle": 0,
    "no_sl_valid": 0,
    "signals_generated": 0,
    "breakout_signals": 0,
    "pullback_signals": 0,
    "london_no_range": 0,
    "london_wrong_hour": 0,
    "london_no_break": 0,
    "london_trend_filter": 0,
    "smc_fvg_confluence": 0,
    "smc_liq_sweep": 0,
    "smc_ob_sl": 0,
    "smc_no_confluence": 0,
}


def reset_diagnostics():
    for k in diag:
        diag[k] = 0


def get_signal(h1: pd.DataFrame, h4: pd.DataFrame, cfg: dict,
               current_ask: float = 0, current_bid: float = 0) -> Optional[Signal]:
    """Route to the appropriate strategy based on config."""
    s = cfg["strategy"]
    if s.get("london_breakout", False):
        return _london_breakout(h1, cfg, current_ask, current_bid)
    return _momentum_breakout(h1, cfg, current_ask, current_bid)


# =====================================================================
# LONDON BREAKOUT STRATEGY
# =====================================================================
def _london_breakout(h1: pd.DataFrame, cfg: dict,
                     current_ask: float, current_bid: float) -> Optional[Signal]:
    """
    Session Range Breakout: trade breakouts of consolidation ranges.
    Supports dual session: London (Asian range) + NY (European range).
    """
    s = cfg["strategy"]
    rr_ratio = s["rr_ratio"]
    min_sl = s.get("min_sl_pips", 15)

    if len(h1) < 30:
        return None

    diag["total_checks"] += 1

    b0 = h1.iloc[-1]
    bar_time = h1.index[-1]
    bar_hour = bar_time.hour
    close = b0["close"]
    atr = b0.get("atr", 0)

    if atr == 0:
        diag["no_h4_indicators"] += 1
        return None

    # Determine which session we're in
    london_start = s.get("london_entry_start", 7)
    london_end = s.get("london_entry_end", 10)
    ny_start = s.get("ny_entry_start", 13)
    ny_end = s.get("ny_entry_end", 16)
    dual = s.get("dual_session", False)

    in_london = london_start <= bar_hour < london_end
    in_ny = dual and (ny_start <= bar_hour < ny_end)

    if not in_london and not in_ny:
        diag["london_wrong_hour"] += 1
        return None

    # Calculate the appropriate consolidation range
    today = bar_time.normalize()

    if in_london:
        # Asian session range (00:00 - 06:00 UTC)
        range_start_h = s.get("asian_start_hour", 0)
        range_end_h = s.get("asian_end_hour", 6)
        session_tag = "LONDON"
    else:
        # European morning range (07:00 - 12:00 UTC)
        range_start_h = s.get("ny_range_start", 7)
        range_end_h = s.get("ny_range_end", 12)
        session_tag = "NY"

    # Get bars from the consolidation range
    range_mask = (h1.index >= today) & (h1.index.hour >= range_start_h) & (h1.index.hour < range_end_h)
    range_bars = h1[range_mask]

    if len(range_bars) < 3:
        yesterday = today - pd.Timedelta(days=1)
        range_mask = (h1.index >= yesterday) & (h1.index < today) & \
                     (h1.index.hour >= range_start_h) & (h1.index.hour < range_end_h)
        range_bars = h1[range_mask]

    if len(range_bars) < 3:
        diag["london_no_range"] += 1
        return None

    asian_high = range_bars["high"].max()
    asian_low = range_bars["low"].min()
    asian_range = asian_high - asian_low
    asian_range_pips = price_to_pips(asian_range)

    # Range size filter
    min_range = s.get("min_asian_range_pips", 15)
    max_range = s.get("max_asian_range_pips", 60)
    if asian_range_pips < min_range or asian_range_pips > max_range:
        diag["london_no_range"] += 1
        return None

    # Optional: EMA200 trend bias
    ema200 = b0.get("ema_trend", 0)
    use_trend_filter = s.get("london_trend_filter", False)

    # Buffer beyond the range for SL
    buffer = atr * s.get("range_buffer_atr", 0.1)

    # Candle quality checks
    bar_range = b0["high"] - b0["low"]
    body_size = abs(close - b0["open"])
    body_ratio = body_size / bar_range if bar_range > 0 else 0

    # ---------------------------------------------------------------
    # MODE A: RETEST ENTRY (wait for pullback after breakout)
    # ---------------------------------------------------------------
    use_retest = s.get("london_retest", False)

    if use_retest:
        retest_lb = s.get("retest_lookback", 5)
        # Look back at recent bars within entry window for a prior breakout
        recent_bars = h1.iloc[-(retest_lb + 1):-1]  # exclude current bar

        buy_breakout_found = False
        sell_breakout_found = False

        for idx in range(len(recent_bars)):
            rb = recent_bars.iloc[idx]
            rb_time = recent_bars.index[idx]
            rb_hour = rb_time.hour
            if rb_hour < london_start or rb_hour >= london_end:
                continue
            if rb["close"] > asian_high and rb["close"] > rb["open"]:
                buy_breakout_found = True
            if rb["close"] < asian_low and rb["close"] < rb["open"]:
                sell_breakout_found = True

        # BUY RETEST: prior breakout above Asian high, current bar retests
        if buy_breakout_found and close > asian_high:
            # Current bar must have dipped toward the Asian high (retest)
            dipped = b0["low"] <= asian_high + atr * 0.3
            bounced = close > asian_high and close > b0["open"]

            if dipped and bounced:
                if use_trend_filter and ema200 > 0 and close < ema200:
                    diag["london_trend_filter"] += 1
                elif body_ratio < 0.25:
                    diag["no_candle"] += 1
                else:
                    entry = current_ask if current_ask > 0 else close
                    sl = b0["low"] - buffer
                    sl_dist = entry - sl
                    sl_pips = price_to_pips(sl_dist)

                    if min_sl <= sl_pips <= 50:
                        tp = entry + sl_dist * rr_ratio
                        diag["signals_generated"] += 1
                        diag["breakout_signals"] += 1
                        return Signal(
                            direction=1, entry=round(entry, 5),
                            sl=round(sl, 5), tp=round(tp, 5),
                            sl_pips=round(sl_pips, 1),
                            tp_pips=round(price_to_pips(tp - entry), 1),
                            reason=f"BUY_RETEST|Range={asian_range_pips:.0f}p"
                        )
                    else:
                        diag["no_sl_valid"] += 1

        # SELL RETEST: prior breakout below Asian low, current bar retests
        if sell_breakout_found and close < asian_low:
            dipped = b0["high"] >= asian_low - atr * 0.3
            bounced = close < asian_low and close < b0["open"]

            if dipped and bounced:
                if use_trend_filter and ema200 > 0 and close > ema200:
                    diag["london_trend_filter"] += 1
                elif body_ratio < 0.25:
                    diag["no_candle"] += 1
                else:
                    entry = current_bid if current_bid > 0 else close
                    sl = b0["high"] + buffer
                    sl_dist = sl - entry
                    sl_pips = price_to_pips(sl_dist)

                    if min_sl <= sl_pips <= 50:
                        tp = entry - sl_dist * rr_ratio
                        diag["signals_generated"] += 1
                        diag["breakout_signals"] += 1
                        return Signal(
                            direction=-1, entry=round(entry, 5),
                            sl=round(sl, 5), tp=round(tp, 5),
                            sl_pips=round(sl_pips, 1),
                            tp_pips=round(price_to_pips(entry - tp), 1),
                            reason=f"SELL_RETEST|Range={asian_range_pips:.0f}p"
                        )
                    else:
                        diag["no_sl_valid"] += 1

        # Also still allow raw breakout on the FIRST breakout bar
        # (immediate momentum entry when it's very strong)
        if close > asian_high and not buy_breakout_found:
            if body_ratio >= 0.5 and close > b0["open"]:
                if not (use_trend_filter and ema200 > 0 and close < ema200):
                    entry = current_ask if current_ask > 0 else close
                    sl = asian_low - buffer
                    sl_dist = entry - sl
                    sl_pips = price_to_pips(sl_dist)
                    if min_sl <= sl_pips <= 80:
                        tp = entry + sl_dist * rr_ratio
                        diag["signals_generated"] += 1
                        diag["pullback_signals"] += 1  # track separately
                        return Signal(
                            direction=1, entry=round(entry, 5),
                            sl=round(sl, 5), tp=round(tp, 5),
                            sl_pips=round(sl_pips, 1),
                            tp_pips=round(price_to_pips(tp - entry), 1),
                            reason=f"BUY_LONDON|Range={asian_range_pips:.0f}p"
                        )

        if close < asian_low and not sell_breakout_found:
            if body_ratio >= 0.5 and close < b0["open"]:
                if not (use_trend_filter and ema200 > 0 and close > ema200):
                    entry = current_bid if current_bid > 0 else close
                    sl = asian_high + buffer
                    sl_dist = sl - entry
                    sl_pips = price_to_pips(sl_dist)
                    if min_sl <= sl_pips <= 80:
                        tp = entry - sl_dist * rr_ratio
                        diag["signals_generated"] += 1
                        diag["pullback_signals"] += 1
                        return Signal(
                            direction=-1, entry=round(entry, 5),
                            sl=round(sl, 5), tp=round(tp, 5),
                            sl_pips=round(sl_pips, 1),
                            tp_pips=round(price_to_pips(entry - tp), 1),
                            reason=f"SELL_LONDON|Range={asian_range_pips:.0f}p"
                        )

        diag["london_no_break"] += 1
        return None

    # ---------------------------------------------------------------
    # MODE B: SMC-ENHANCED BREAKOUT
    # ---------------------------------------------------------------
    # SMC config
    use_fvg = s.get("smc_fvg_filter", False)
    fvg_lb = s.get("smc_fvg_lookback", 12)
    use_liq_sweep = s.get("smc_liq_sweep_bonus", False)
    liq_lb = s.get("smc_liq_lookback", 8)
    use_ob_sl = s.get("smc_ob_sl", False)
    require_conf = s.get("smc_require_confluence", False)

    # Current bar index in the DataFrame
    bar_idx = len(h1) - 1

    # === BUY: close breaks above Asian high ===
    if close > asian_high:
        if use_trend_filter and ema200 > 0 and close < ema200:
            diag["london_trend_filter"] += 1
        elif body_ratio < 0.3 or close <= b0["open"]:
            diag["no_candle"] += 1
        else:
            # --- SMC Confluence Check ---
            smc_tags = []
            has_fvg = has_recent_fvg(h1, bar_idx, 1, fvg_lb) if use_fvg else False
            has_sweep = has_recent_liquidity_sweep(h1, bar_idx, 1, liq_lb) if use_liq_sweep else False

            if has_fvg:
                smc_tags.append("FVG")
                diag["smc_fvg_confluence"] += 1
            if has_sweep:
                smc_tags.append("SWEEP")
                diag["smc_liq_sweep"] += 1

            # If confluence required but none found → skip
            if require_conf and not smc_tags:
                diag["smc_no_confluence"] += 1
            else:
                entry = current_ask if current_ask > 0 else close

                # --- SL: try Order Block first, fallback to Asian low ---
                sl = asian_low - buffer
                if use_ob_sl:
                    ob_sl = get_nearest_ob_sl(h1, bar_idx, 1, entry)
                    if ob_sl > 0:
                        sl = ob_sl - buffer * 0.5
                        smc_tags.append("OB_SL")
                        diag["smc_ob_sl"] += 1

                sl_dist = entry - sl
                sl_pips = price_to_pips(sl_dist)

                if min_sl <= sl_pips <= 80:
                    # Adjust RR based on SMC confluence
                    effective_rr = rr_ratio
                    if len(smc_tags) >= 2:
                        effective_rr = rr_ratio * 1.15  # Boost TP when strong confluence
                    tp = entry + sl_dist * effective_rr

                    smc_str = "+".join(smc_tags) if smc_tags else "RAW"
                    diag["signals_generated"] += 1
                    diag["breakout_signals"] += 1
                    return Signal(
                        direction=1, entry=round(entry, 5),
                        sl=round(sl, 5), tp=round(tp, 5),
                        sl_pips=round(sl_pips, 1),
                        tp_pips=round(price_to_pips(tp - entry), 1),
                        reason=f"BUY_{session_tag}|{smc_str}|R={asian_range_pips:.0f}p"
                    )
                else:
                    diag["no_sl_valid"] += 1
                    return None

    # === SELL: close breaks below Asian low ===
    if close < asian_low:
        if use_trend_filter and ema200 > 0 and close > ema200:
            diag["london_trend_filter"] += 1
        elif body_ratio < 0.3 or close >= b0["open"]:
            diag["no_candle"] += 1
        else:
            # --- SMC Confluence Check ---
            smc_tags = []
            has_fvg = has_recent_fvg(h1, bar_idx, -1, fvg_lb) if use_fvg else False
            has_sweep = has_recent_liquidity_sweep(h1, bar_idx, -1, liq_lb) if use_liq_sweep else False

            if has_fvg:
                smc_tags.append("FVG")
                diag["smc_fvg_confluence"] += 1
            if has_sweep:
                smc_tags.append("SWEEP")
                diag["smc_liq_sweep"] += 1

            if require_conf and not smc_tags:
                diag["smc_no_confluence"] += 1
            else:
                entry = current_bid if current_bid > 0 else close

                # --- SL: try Order Block first, fallback to Asian high ---
                sl = asian_high + buffer
                if use_ob_sl:
                    ob_sl = get_nearest_ob_sl(h1, bar_idx, -1, entry)
                    if ob_sl > 0:
                        sl = ob_sl + buffer * 0.5
                        smc_tags.append("OB_SL")
                        diag["smc_ob_sl"] += 1

                sl_dist = sl - entry
                sl_pips = price_to_pips(sl_dist)

                if min_sl <= sl_pips <= 80:
                    effective_rr = rr_ratio
                    if len(smc_tags) >= 2:
                        effective_rr = rr_ratio * 1.15
                    tp = entry - sl_dist * effective_rr

                    smc_str = "+".join(smc_tags) if smc_tags else "RAW"
                    diag["signals_generated"] += 1
                    diag["breakout_signals"] += 1
                    return Signal(
                        direction=-1, entry=round(entry, 5),
                        sl=round(sl, 5), tp=round(tp, 5),
                        sl_pips=round(sl_pips, 1),
                        tp_pips=round(price_to_pips(entry - tp), 1),
                        reason=f"SELL_{session_tag}|{smc_str}|R={asian_range_pips:.0f}p"
                    )
                else:
                    diag["no_sl_valid"] += 1
                    return None

    diag["london_no_break"] += 1
    return None


# =====================================================================
# MOMENTUM BREAKOUT STRATEGY (original)
# =====================================================================
def _momentum_breakout(h1: pd.DataFrame, cfg: dict,
                       current_ask: float, current_bid: float) -> Optional[Signal]:
    """H1 EMA200 trend + Donchian breakout / EMA21 pullback."""
    s = cfg["strategy"]
    breakout_bars = s.get("breakout_bars", 20)
    slope_bars = s.get("h4_slope_bars", 10)
    min_sl = s.get("min_sl_pips", 5)

    if len(h1) < max(breakout_bars + 3, 50):
        return None

    diag["total_checks"] += 1

    b0 = h1.iloc[-1]
    close = b0["close"]
    ema200 = b0.get("ema_trend", 0)
    ema50 = b0.get("ema_slow", 0)
    ema21 = b0.get("ema_fast", 0)
    adx = b0.get("adx", 0)
    plus_di = b0.get("plus_di", 0)
    minus_di = b0.get("minus_di", 0)
    rsi = b0.get("rsi", 50)
    atr = b0.get("atr", 0)

    if ema200 == 0 or ema50 == 0 or ema21 == 0 or atr == 0 or adx == 0:
        diag["no_h4_indicators"] += 1
        return None

    ema200_prev = h1.iloc[-(slope_bars + 1)].get("ema_trend", 0)
    if ema200_prev == 0:
        diag["no_h4_indicators"] += 1
        return None

    ema200_rising = ema200 > ema200_prev
    ema200_falling = ema200 < ema200_prev
    adx_thresh = s["adx_threshold"]
    di_sep = s.get("di_separation", 0)

    uptrend = (close > ema200 and ema50 > ema200 and ema200_rising and
               adx > adx_thresh and plus_di > minus_di + di_sep)
    downtrend = (close < ema200 and ema50 < ema200 and ema200_falling and
                 adx > adx_thresh and minus_di > plus_di + di_sep)

    if not uptrend and not downtrend:
        diag["no_h4_trend"] += 1
        return None

    rr_ratio = s["rr_ratio"]
    lookback = h1.iloc[-(breakout_bars + 1):-1]
    if len(lookback) < breakout_bars:
        return None

    bar_range = b0["high"] - b0["low"]
    body_size = abs(close - b0["open"])
    body_ratio = body_size / bar_range if bar_range > 0 else 0

    # === BUY ===
    if uptrend:
        prev_high = lookback["high"].max()
        if close > prev_high:
            if rsi > s.get("rsi_breakout_max", 75):
                diag["no_rsi"] += 1
                return None
            if body_ratio < 0.4 or close <= b0["open"]:
                diag["no_candle"] += 1
                return None

            entry = current_ask if current_ask > 0 else close
            sl_dist = atr * s.get("breakout_atr_sl", 1.5)
            sl = entry - sl_dist
            tp = entry + sl_dist * rr_ratio
            sl_pips = price_to_pips(sl_dist)
            if min_sl <= sl_pips <= 80:
                diag["signals_generated"] += 1
                diag["breakout_signals"] += 1
                return Signal(
                    direction=1, entry=round(entry, 5),
                    sl=round(sl, 5), tp=round(tp, 5),
                    sl_pips=round(sl_pips, 1),
                    tp_pips=round(price_to_pips(tp - entry), 1),
                    reason=f"BUY_BREAK|{breakout_bars}b|RSI={rsi:.0f}|ADX={adx:.0f}"
                )
            else:
                diag["no_sl_valid"] += 1

        # Fallback: pullback
        b1 = h1.iloc[-2]
        ema21_rising = ema21 > b1.get("ema_fast", 0)
        if ema21_rising and b1["close"] <= ema21 and close > ema21 + atr * 0.05:
            rsi_ok = s["rsi_buy_low"] <= rsi <= s["rsi_buy_high"]
            bullish = close > b0["open"] and body_ratio >= 0.3
            if rsi_ok and bullish:
                swing_low = min(h1.iloc[k]["low"] for k in range(-5, 0))
                sl = swing_low - atr * 0.5
                entry = current_ask if current_ask > 0 else close
                sl_dist = entry - sl
                sl_pip = price_to_pips(sl_dist)
                if atr * 0.3 <= sl_dist <= atr * 4.0 and min_sl <= sl_pip <= 80:
                    tp = entry + sl_dist * rr_ratio
                    diag["signals_generated"] += 1
                    diag["pullback_signals"] += 1
                    return Signal(
                        direction=1, entry=round(entry, 5),
                        sl=round(sl, 5), tp=round(tp, 5),
                        sl_pips=round(sl_pip, 1),
                        tp_pips=round(price_to_pips(tp - entry), 1),
                        reason=f"BUY_PULL|RSI={rsi:.0f}|ADX={adx:.0f}"
                    )
                else:
                    diag["no_sl_valid"] += 1

    # === SELL ===
    if downtrend:
        prev_low = lookback["low"].min()
        if close < prev_low:
            if rsi < s.get("rsi_breakout_min", 25):
                diag["no_rsi"] += 1
                return None
            if body_ratio < 0.4 or close >= b0["open"]:
                diag["no_candle"] += 1
                return None

            entry = current_bid if current_bid > 0 else close
            sl_dist = atr * s.get("breakout_atr_sl", 1.5)
            sl = entry + sl_dist
            tp = entry - sl_dist * rr_ratio
            sl_pips = price_to_pips(sl_dist)
            if min_sl <= sl_pips <= 80:
                diag["signals_generated"] += 1
                diag["breakout_signals"] += 1
                return Signal(
                    direction=-1, entry=round(entry, 5),
                    sl=round(sl, 5), tp=round(tp, 5),
                    sl_pips=round(sl_pips, 1),
                    tp_pips=round(price_to_pips(entry - tp), 1),
                    reason=f"SELL_BREAK|{breakout_bars}b|RSI={rsi:.0f}|ADX={adx:.0f}"
                )
            else:
                diag["no_sl_valid"] += 1

        # Fallback: pullback
        b1 = h1.iloc[-2]
        ema21_falling = ema21 < b1.get("ema_fast", 0)
        if ema21_falling and b1["close"] >= ema21 and close < ema21 - atr * 0.05:
            rsi_ok = s["rsi_sell_low"] <= rsi <= s["rsi_sell_high"]
            bearish = close < b0["open"] and body_ratio >= 0.3
            if rsi_ok and bearish:
                swing_high = max(h1.iloc[k]["high"] for k in range(-5, 0))
                sl = swing_high + atr * 0.5
                entry = current_bid if current_bid > 0 else close
                sl_dist = sl - entry
                sl_pip = price_to_pips(sl_dist)
                if atr * 0.3 <= sl_dist <= atr * 4.0 and min_sl <= sl_pip <= 80:
                    tp = entry - sl_dist * rr_ratio
                    diag["signals_generated"] += 1
                    diag["pullback_signals"] += 1
                    return Signal(
                        direction=-1, entry=round(entry, 5),
                        sl=round(sl, 5), tp=round(tp, 5),
                        sl_pips=round(sl_pip, 1),
                        tp_pips=round(price_to_pips(entry - tp), 1),
                        reason=f"SELL_PULL|RSI={rsi:.0f}|ADX={adx:.0f}"
                    )
                else:
                    diag["no_sl_valid"] += 1

    diag["no_pullback"] += 1
    return None
