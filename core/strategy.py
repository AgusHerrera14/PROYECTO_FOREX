"""
core/strategy.py - Multi-strategy trading module.

Three independent strategies that trade in DIFFERENT market conditions:

1. London/NY Breakout (primary):
   - Asian range breakout during London (07-11 UTC)
   - European range breakout during NY (13-16 UTC)
   - SMC confluence filters for quality

2. Trend Pullback (secondary):
   - EMA21 pullback in strong H1+H4 trends
   - Active during London + NY hours (07-17 UTC)

3. Mean Reversion (complementary):
   - Fade Bollinger Band extremes in ranging markets (ADX < 20)
   - Active during Asian (00-07 UTC) + late session (17-22 UTC)
   - Higher win rate, lower R:R — diversifies the portfolio
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
    strategy: str = ""


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
    "meanrev_signals": 0,
    "london_no_range": 0,
    "london_wrong_hour": 0,
    "london_no_break": 0,
    "london_trend_filter": 0,
    "smc_fvg_confluence": 0,
    "smc_liq_sweep": 0,
    "smc_ob_sl": 0,
    "smc_no_confluence": 0,
    "mr_wrong_hour": 0,
    "mr_not_ranging": 0,
    "mr_no_touch": 0,
}


def reset_diagnostics():
    for k in diag:
        diag[k] = 0


def get_signal(h1: pd.DataFrame, h4: pd.DataFrame, cfg: dict,
               current_ask: float = 0, current_bid: float = 0) -> Optional[Signal]:
    """
    Try strategies in priority order:
    1. London/NY Breakout (during breakout windows)
    2. Trend Pullback (during London+NY hours, in trending markets)
    3. Mean Reversion (during Asian/late hours, in ranging markets)
    """
    s = cfg["strategy"]
    signal = None

    # Primary: London / NY Breakout
    if s.get("london_breakout", False):
        signal = _london_breakout(h1, cfg, current_ask, current_bid)

    # Secondary: Trend pullback with H4 confirmation
    if signal is None and s.get("momentum_fallback", False):
        bar_hour = h1.index[-1].hour
        if 7 <= bar_hour < 17:
            signal = _trend_pullback(h1, h4, cfg, current_ask, current_bid)

    # Tertiary: Mean reversion in ranging markets
    if signal is None and s.get("mean_reversion", False):
        signal = _mean_reversion(h1, cfg, current_ask, current_bid)

    # If no London breakout configured, use pullback directly
    if signal is None and not s.get("london_breakout", False):
        signal = _trend_pullback(h1, h4, cfg, current_ask, current_bid)

    return signal


# =====================================================================
# STRATEGY 1: LONDON / NY BREAKOUT (original proven strategy)
# =====================================================================
def _london_breakout(h1: pd.DataFrame, cfg: dict,
                     current_ask: float, current_bid: float) -> Optional[Signal]:
    """
    Session Range Breakout: trade breakouts of consolidation ranges.
    Supports dual session: London (Asian range) + NY (European range).
    ORIGINAL proven parameters: PF 1.21, 51.3% WR.
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
        range_start_h = s.get("asian_start_hour", 0)
        range_end_h = s.get("asian_end_hour", 6)
        session_tag = "LONDON"
    else:
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

    # SMC config
    use_fvg = s.get("smc_fvg_filter", False)
    fvg_lb = s.get("smc_fvg_lookback", 12)
    use_liq_sweep = s.get("smc_liq_sweep_bonus", False)
    liq_lb = s.get("smc_liq_lookback", 8)
    use_ob_sl = s.get("smc_ob_sl", False)
    require_conf = s.get("smc_require_confluence", False)

    bar_idx = len(h1) - 1

    # Quality filter indicators
    adx = b0.get("adx", 0)
    rsi = b0.get("rsi", 50)
    ema21 = b0.get("ema_fast", 0)
    ema50 = b0.get("ema_slow", 0)

    # Breakout magnitude
    min_breakout_pips = 2.0

    # === BUY: close breaks above Asian high ===
    breakout_pips_buy = price_to_pips(close - asian_high)
    if close > asian_high and breakout_pips_buy >= min_breakout_pips:
        if use_trend_filter and ema200 > 0 and close < ema200:
            diag["london_trend_filter"] += 1
        elif body_ratio < 0.4 or close <= b0["open"]:
            diag["no_candle"] += 1
        elif ema21 > 0 and ema50 > 0 and ema21 < ema50:
            diag["no_h4_slope"] += 1
        elif rsi > 75:
            diag["no_rsi"] += 1
        else:
            smc_tags = []
            has_fvg = has_recent_fvg(h1, bar_idx, 1, fvg_lb) if use_fvg else False
            has_sweep = has_recent_liquidity_sweep(h1, bar_idx, 1, liq_lb) if use_liq_sweep else False

            if has_fvg:
                smc_tags.append("FVG")
                diag["smc_fvg_confluence"] += 1
            if has_sweep:
                smc_tags.append("SWEEP")
                diag["smc_liq_sweep"] += 1

            if require_conf and not smc_tags:
                diag["smc_no_confluence"] += 1
            else:
                entry = current_ask if current_ask > 0 else close
                atr_sl = entry - atr * s.get("breakout_atr_sl", 1.2)
                range_sl = asian_low - buffer
                sl = max(atr_sl, range_sl)

                if use_ob_sl:
                    ob_sl = get_nearest_ob_sl(h1, bar_idx, 1, entry)
                    if ob_sl > 0 and ob_sl > sl:
                        sl = ob_sl - buffer * 0.5
                        smc_tags.append("OB_SL")
                        diag["smc_ob_sl"] += 1

                sl_dist = entry - sl
                sl_pips = price_to_pips(sl_dist)

                if min_sl <= sl_pips <= 35:
                    effective_rr = rr_ratio
                    if len(smc_tags) >= 2:
                        effective_rr = rr_ratio * 1.15
                    tp = entry + sl_dist * effective_rr

                    smc_str = "+".join(smc_tags) if smc_tags else "RAW"
                    diag["signals_generated"] += 1
                    diag["breakout_signals"] += 1
                    return Signal(
                        direction=1, entry=round(entry, 5),
                        sl=round(sl, 5), tp=round(tp, 5),
                        sl_pips=round(sl_pips, 1),
                        tp_pips=round(price_to_pips(tp - entry), 1),
                        reason=f"BUY_{session_tag}|{smc_str}|R={asian_range_pips:.0f}p",
                        strategy="Breakout",
                    )
                else:
                    diag["no_sl_valid"] += 1
                    return None

    # === SELL: close breaks below Asian low ===
    breakout_pips_sell = price_to_pips(asian_low - close)
    if close < asian_low and breakout_pips_sell >= min_breakout_pips:
        if use_trend_filter and ema200 > 0 and close > ema200:
            diag["london_trend_filter"] += 1
        elif body_ratio < 0.4 or close >= b0["open"]:
            diag["no_candle"] += 1
        elif ema21 > 0 and ema50 > 0 and ema21 > ema50:
            diag["no_h4_slope"] += 1
        elif rsi < 25:
            diag["no_rsi"] += 1
        else:
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
                atr_sl = entry + atr * s.get("breakout_atr_sl", 1.2)
                range_sl = asian_high + buffer
                sl = min(atr_sl, range_sl)

                if use_ob_sl:
                    ob_sl = get_nearest_ob_sl(h1, bar_idx, -1, entry)
                    if ob_sl > 0 and ob_sl < sl:
                        sl = ob_sl + buffer * 0.5
                        smc_tags.append("OB_SL")
                        diag["smc_ob_sl"] += 1

                sl_dist = sl - entry
                sl_pips = price_to_pips(sl_dist)

                if min_sl <= sl_pips <= 35:
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
                        reason=f"SELL_{session_tag}|{smc_str}|R={asian_range_pips:.0f}p",
                        strategy="Breakout",
                    )
                else:
                    diag["no_sl_valid"] += 1
                    return None

    diag["london_no_break"] += 1
    return None


# =====================================================================
# STRATEGY 2: TREND PULLBACK (H4-confirmed)
# =====================================================================
def _trend_pullback(h1: pd.DataFrame, h4: pd.DataFrame, cfg: dict,
                    current_ask: float, current_bid: float) -> Optional[Signal]:
    """
    EMA21 pullback in confirmed H1+H4 trends.
    Higher quality entries through multi-timeframe alignment.
    """
    s = cfg["strategy"]
    min_sl = s.get("min_sl_pips", 10)
    pullback_bars = s.get("pullback_bars", 5)

    if len(h1) < 50:
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

    # H1 trend filters
    adx_thresh = s.get("adx_threshold", 25)
    ema_aligned_up = (ema21 > ema50 > ema200 and close > ema200)
    ema_aligned_down = (ema21 < ema50 < ema200 and close < ema200)
    adx_ok = adx > adx_thresh
    di_buy = plus_di > minus_di
    di_sell = minus_di > plus_di

    uptrend = ema_aligned_up and adx_ok and di_buy
    downtrend = ema_aligned_down and adx_ok and di_sell

    if not uptrend and not downtrend:
        diag["no_h4_trend"] += 1
        return None

    # H4 multi-timeframe confirmation
    if h4 is not None and len(h4) >= 5:
        h4_bar = h4.iloc[-1]
        h4_ema_slow = h4_bar.get("ema_slow", 0)
        h4_ema_trend = h4_bar.get("ema_trend", 0)
        h4_adx = h4_bar.get("adx", 0)
        h4_plus = h4_bar.get("plus_di", 0)
        h4_minus = h4_bar.get("minus_di", 0)

        if h4_ema_slow > 0 and h4_ema_trend > 0 and h4_adx > 0:
            if uptrend:
                if not (h4_ema_slow > h4_ema_trend and h4_adx > 18 and h4_plus > h4_minus):
                    diag["no_h4_trend"] += 1
                    return None
            elif downtrend:
                if not (h4_ema_slow < h4_ema_trend and h4_adx > 18 and h4_minus > h4_plus):
                    diag["no_h4_trend"] += 1
                    return None

    rr_ratio = s["rr_ratio"]
    bar_range = b0["high"] - b0["low"]
    body_size = abs(close - b0["open"])
    body_ratio = body_size / bar_range if bar_range > 0 else 0

    # Check last 3 bars for pullback to EMA21
    b1 = h1.iloc[-2]

    # === BUY PULLBACK ===
    if uptrend:
        ema_zone = ema21 + atr * 0.20
        pullback_found = False
        for k in range(-3, 0):
            if abs(k) <= len(h1):
                bar_k = h1.iloc[k]
                if bar_k["low"] <= ema_zone:
                    pullback_found = True
                    break

        bounced = close > ema21 and close > b0["open"]

        if pullback_found and bounced:
            rsi_lo = s.get("rsi_buy_low", 25)
            rsi_hi = s.get("rsi_buy_high", 65)
            if not (rsi_lo <= rsi <= rsi_hi):
                diag["no_rsi"] += 1
                return None

            if body_ratio < 0.25:
                diag["no_candle"] += 1
                return None

            swing_low = min(h1.iloc[k]["low"] for k in range(-pullback_bars, 0))
            sl = swing_low - atr * 0.3

            entry = current_ask if current_ask > 0 else close
            sl_dist = entry - sl
            sl_pips = price_to_pips(sl_dist)

            if min_sl <= sl_pips <= 35:
                tp = entry + sl_dist * rr_ratio
                diag["signals_generated"] += 1
                diag["pullback_signals"] += 1
                return Signal(
                    direction=1, entry=round(entry, 5),
                    sl=round(sl, 5), tp=round(tp, 5),
                    sl_pips=round(sl_pips, 1),
                    tp_pips=round(price_to_pips(tp - entry), 1),
                    reason=f"BUY_PULL|RSI={rsi:.0f}|ADX={adx:.0f}",
                    strategy="Pullback",
                )
            else:
                diag["no_sl_valid"] += 1

    # === SELL PULLBACK ===
    if downtrend:
        ema_zone = ema21 - atr * 0.20
        pullback_found = False
        for k in range(-3, 0):
            if abs(k) <= len(h1):
                bar_k = h1.iloc[k]
                if bar_k["high"] >= ema_zone:
                    pullback_found = True
                    break

        bounced = close < ema21 and close < b0["open"]

        if pullback_found and bounced:
            rsi_lo = s.get("rsi_sell_low", 35)
            rsi_hi = s.get("rsi_sell_high", 75)
            if not (rsi_lo <= rsi <= rsi_hi):
                diag["no_rsi"] += 1
                return None

            if body_ratio < 0.25:
                diag["no_candle"] += 1
                return None

            swing_high = max(h1.iloc[k]["high"] for k in range(-pullback_bars, 0))
            sl = swing_high + atr * 0.3

            entry = current_bid if current_bid > 0 else close
            sl_dist = sl - entry
            sl_pips = price_to_pips(sl_dist)

            if min_sl <= sl_pips <= 35:
                tp = entry - sl_dist * rr_ratio
                diag["signals_generated"] += 1
                diag["pullback_signals"] += 1
                return Signal(
                    direction=-1, entry=round(entry, 5),
                    sl=round(sl, 5), tp=round(tp, 5),
                    sl_pips=round(sl_pips, 1),
                    tp_pips=round(price_to_pips(entry - tp), 1),
                    reason=f"SELL_PULL|RSI={rsi:.0f}|ADX={adx:.0f}",
                    strategy="Pullback",
                )
            else:
                diag["no_sl_valid"] += 1

    diag["no_pullback"] += 1
    return None


# =====================================================================
# STRATEGY 3: MEAN REVERSION (Bollinger Band fade)
# =====================================================================
def _mean_reversion(h1: pd.DataFrame, cfg: dict,
                    current_ask: float, current_bid: float) -> Optional[Signal]:
    """
    Fade Bollinger Band extremes in ranging markets (ADX < 20).
    Active during Asian session (00-07) and late session (17-22).
    Higher win rate (~60-65%) with R:R ~1.0 to diversify portfolio.
    """
    s = cfg["strategy"]

    if len(h1) < 30:
        return None

    b0 = h1.iloc[-1]
    bar_time = h1.index[-1]
    bar_hour = bar_time.hour

    # Time filter: Asian session + late session (not during London/NY breakout)
    h_start1 = s.get("mr_hours_start", 0)
    h_end1 = s.get("mr_hours_end", 7)
    h_start2 = s.get("mr_hours2_start", 17)
    h_end2 = s.get("mr_hours2_end", 22)

    in_window = (h_start1 <= bar_hour < h_end1) or (h_start2 <= bar_hour < h_end2)
    if not in_window:
        diag["mr_wrong_hour"] += 1
        return None

    diag["total_checks"] += 1

    close = b0["close"]
    open_price = b0["open"]
    atr = b0.get("atr", 0)
    adx = b0.get("adx", 0)
    rsi = b0.get("rsi", 50)
    bb_upper = b0.get("bb_upper", 0)
    bb_lower = b0.get("bb_lower", 0)
    bb_middle = b0.get("bb_middle", 0)
    ema21 = b0.get("ema_fast", 0)

    if atr == 0 or bb_upper == 0 or bb_lower == 0 or bb_middle == 0:
        return None

    # Ranging market filter: ADX must be LOW (no strong trend)
    mr_adx_max = s.get("mr_adx_max", 20)
    if adx > mr_adx_max:
        diag["mr_not_ranging"] += 1
        return None

    # R:R for mean reversion (target = mean/middle band)
    mr_rr = s.get("mr_rr_ratio", 1.0)
    min_sl = s.get("mr_min_sl_pips", 10)
    max_sl = s.get("mr_max_sl_pips", 30)

    bar_range = b0["high"] - b0["low"]
    body_size = abs(close - open_price)
    body_ratio = body_size / bar_range if bar_range > 0 else 0

    bb_range = bb_upper - bb_lower
    if bb_range <= 0:
        return None

    # === BUY: Price at lower Bollinger Band (oversold bounce) ===
    # Close near or below lower BB, RSI oversold, bullish candle
    if close <= bb_lower + atr * 0.3:
        if rsi > 40:  # Not oversold enough
            diag["mr_no_touch"] += 1
            return None
        if close >= open_price:  # Need bearish or doji candle that reached the band
            # Actually for mean reversion buy, we want the bar to have BOUNCED
            # Check: low touched BB lower, but close recovered above it
            if b0["low"] > bb_lower + atr * 0.1:
                diag["mr_no_touch"] += 1
                return None

        # Confirm bounce: close should be above low by decent amount
        if close <= b0["low"] + (bar_range * 0.3):
            diag["mr_no_touch"] += 1
            return None

        entry = current_ask if current_ask > 0 else close
        sl = b0["low"] - atr * 0.5  # SL below the bounce low
        tp = bb_middle  # Target = mean (middle band)

        sl_dist = entry - sl
        tp_dist = tp - entry
        sl_pips = price_to_pips(sl_dist)
        tp_pips = price_to_pips(tp_dist)

        if tp_dist <= 0:
            return None

        # Ensure minimum R:R of 0.8
        actual_rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if actual_rr < 0.8:
            diag["no_sl_valid"] += 1
            return None

        if min_sl <= sl_pips <= max_sl:
            diag["signals_generated"] += 1
            diag["meanrev_signals"] += 1
            return Signal(
                direction=1, entry=round(entry, 5),
                sl=round(sl, 5), tp=round(tp, 5),
                sl_pips=round(sl_pips, 1),
                tp_pips=round(tp_pips, 1),
                reason=f"BUY_MR|RSI={rsi:.0f}|ADX={adx:.0f}",
                strategy="MeanRev",
            )
        else:
            diag["no_sl_valid"] += 1

    # === SELL: Price at upper Bollinger Band (overbought fade) ===
    if close >= bb_upper - atr * 0.3:
        if rsi < 60:  # Not overbought enough
            diag["mr_no_touch"] += 1
            return None
        if close <= open_price:
            if b0["high"] < bb_upper - atr * 0.1:
                diag["mr_no_touch"] += 1
                return None

        # Confirm rejection: close should be below high by decent amount
        if close >= b0["high"] - (bar_range * 0.3):
            diag["mr_no_touch"] += 1
            return None

        entry = current_bid if current_bid > 0 else close
        sl = b0["high"] + atr * 0.5  # SL above the rejection high
        tp = bb_middle  # Target = mean

        sl_dist = sl - entry
        tp_dist = entry - tp
        sl_pips = price_to_pips(sl_dist)
        tp_pips = price_to_pips(tp_dist)

        if tp_dist <= 0:
            return None

        actual_rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if actual_rr < 0.8:
            diag["no_sl_valid"] += 1
            return None

        if min_sl <= sl_pips <= max_sl:
            diag["signals_generated"] += 1
            diag["meanrev_signals"] += 1
            return Signal(
                direction=-1, entry=round(entry, 5),
                sl=round(sl, 5), tp=round(tp, 5),
                sl_pips=round(sl_pips, 1),
                tp_pips=round(tp_pips, 1),
                reason=f"SELL_MR|RSI={rsi:.0f}|ADX={adx:.0f}",
                strategy="MeanRev",
            )
        else:
            diag["no_sl_valid"] += 1

    diag["mr_no_touch"] += 1
    return None
