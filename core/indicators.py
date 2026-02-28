"""
core/indicators.py - Technical indicator calculations.
Pure pandas/numpy, no external TA library needed.
"""
import pandas as pd
import numpy as np


# =====================================================================
# BASE INDICATORS (pure pandas/numpy, replacing pandas_ta)
# =====================================================================

def _ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential Moving Average (Wilder-compatible)."""
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series,
         length: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing)."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series,
         length: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX with +DI and -DI (Wilder's method). Returns (adx, plus_di, minus_di)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    plus_dm = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)
    minus_dm = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)

    atr_vals = _atr(high, low, close, length)

    smooth_plus = plus_dm.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    smooth_minus = minus_dm.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    plus_di = 100.0 * smooth_plus / atr_vals.replace(0, np.nan)
    minus_di = 100.0 * smooth_minus / atr_vals.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    return adx, plus_di, minus_di


# =====================================================================
# PUBLIC API (same interface as before)
# =====================================================================

def calculate_all(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Calculate all indicators on OHLCV DataFrame.
    Expects columns: open, high, low, close, volume (lowercase).
    Returns df with indicator columns added.
    """
    s = cfg["strategy"]

    # --- EMAs ---
    df["ema_fast"] = _ema(df["close"], s["ema_fast"])

    if "ema_slow" not in df.columns:
        df["ema_slow"] = _ema(df["close"], s["ema_slow"])
    if "ema_trend" not in df.columns:
        df["ema_trend"] = _ema(df["close"], s["ema_trend"])

    # --- RSI ---
    df["rsi"] = _rsi(df["close"], s["rsi_period"])

    # --- ATR ---
    df["atr"] = _atr(df["high"], df["low"], df["close"], s["atr_period"])

    # --- ADX with +DI / -DI ---
    adx, plus_di, minus_di = _adx(df["high"], df["low"], df["close"], s["adx_period"])
    df["adx"] = adx
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    return df


def calculate_h4_trend(h4_df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Calculate trend indicators on H4 data."""
    s = cfg["strategy"]
    h4_df["ema_slow"] = _ema(h4_df["close"], s["ema_slow"])
    h4_df["ema_trend"] = _ema(h4_df["close"], s["ema_trend"])

    adx, plus_di, minus_di = _adx(h4_df["high"], h4_df["low"], h4_df["close"], s["adx_period"])
    h4_df["adx"] = adx
    h4_df["plus_di"] = plus_di
    h4_df["minus_di"] = minus_di

    return h4_df


def resample_to_h4(h1_df: pd.DataFrame) -> pd.DataFrame:
    """Resample H1 OHLCV data to H4 bars."""
    h4 = h1_df.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return h4


def pip_size(symbol: str = "EURUSD") -> float:
    """Return pip size for a symbol (5-digit broker)."""
    if "JPY" in symbol:
        return 0.01
    return 0.0001


def price_to_pips(price_distance: float, symbol: str = "EURUSD") -> float:
    """Convert a price distance to pips."""
    ps = pip_size(symbol)
    if ps <= 0:
        return 0.0
    return abs(price_distance) / ps


# =====================================================================
# SMART MONEY CONCEPTS (SMC) INDICATORS
# Implemented from scratch — no external dependency needed.
# Based on ICT methodology: FVG, Order Blocks, Liquidity Sweeps
# =====================================================================

import numpy as np


def detect_fvg(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """
    Detect Fair Value Gaps (FVG) in OHLC data.

    A bullish FVG: candle[i-2].high < candle[i].low (gap up, middle candle big)
    A bearish FVG: candle[i-2].low > candle[i].high (gap down, middle candle big)

    Adds columns:
      fvg_bull: 1.0 where bullish FVG exists, 0.0 otherwise
      fvg_bear: 1.0 where bearish FVG exists, 0.0 otherwise
      fvg_bull_top / fvg_bull_bot: FVG zone boundaries
      fvg_bear_top / fvg_bear_bot: FVG zone boundaries
    """
    n = len(df)
    fvg_bull = np.zeros(n)
    fvg_bear = np.zeros(n)
    fvg_bull_top = np.full(n, np.nan)
    fvg_bull_bot = np.full(n, np.nan)
    fvg_bear_top = np.full(n, np.nan)
    fvg_bear_bot = np.full(n, np.nan)

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values

    for i in range(2, n):
        # Bullish FVG: candle[i-2] high < candle[i] low
        # Middle candle must be bullish and large
        if lows[i] > highs[i - 2]:
            mid_body = abs(closes[i - 1] - opens[i - 1])
            mid_range = highs[i - 1] - lows[i - 1]
            if mid_range > 0 and mid_body / mid_range > 0.3:
                fvg_bull[i] = 1.0
                fvg_bull_top[i] = lows[i]       # top of the gap
                fvg_bull_bot[i] = highs[i - 2]   # bottom of the gap

        # Bearish FVG: candle[i-2] low > candle[i] high
        if highs[i] < lows[i - 2]:
            mid_body = abs(closes[i - 1] - opens[i - 1])
            mid_range = highs[i - 1] - lows[i - 1]
            if mid_range > 0 and mid_body / mid_range > 0.3:
                fvg_bear[i] = 1.0
                fvg_bear_top[i] = lows[i - 2]    # top of the gap
                fvg_bear_bot[i] = highs[i]        # bottom of the gap

    df["fvg_bull"] = fvg_bull
    df["fvg_bear"] = fvg_bear
    df["fvg_bull_top"] = fvg_bull_top
    df["fvg_bull_bot"] = fvg_bull_bot
    df["fvg_bear_top"] = fvg_bear_top
    df["fvg_bear_bot"] = fvg_bear_bot

    return df


def detect_order_blocks(df: pd.DataFrame, swing_len: int = 10) -> pd.DataFrame:
    """
    Detect Order Blocks (OB) — the last opposing candle before a significant move.

    Bullish OB: last bearish candle before a strong bullish displacement
    Bearish OB: last bullish candle before a strong bearish displacement

    Adds columns:
      ob_bull_top / ob_bull_bot: nearest unmitigated bullish OB zone
      ob_bear_top / ob_bear_bot: nearest unmitigated bearish OB zone
    """
    n = len(df)
    ob_bull_top = np.full(n, np.nan)
    ob_bull_bot = np.full(n, np.nan)
    ob_bear_top = np.full(n, np.nan)
    ob_bear_bot = np.full(n, np.nan)

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values
    atr = df["atr"].values if "atr" in df.columns else np.ones(n) * 0.001

    # Track active (unmitigated) order blocks
    active_bull_obs = []  # list of (top, bot) tuples
    active_bear_obs = []

    for i in range(swing_len, n):
        cur_atr = atr[i] if atr[i] > 0 else 0.001

        # Check for displacement (strong move = body > 1.5 * ATR)
        body = abs(closes[i] - opens[i])
        is_bullish_displacement = closes[i] > opens[i] and body > cur_atr * 1.2
        is_bearish_displacement = closes[i] < opens[i] and body > cur_atr * 1.2

        # Bullish OB: find last bearish candle before bullish displacement
        if is_bullish_displacement:
            for j in range(i - 1, max(i - swing_len, 0) - 1, -1):
                if closes[j] < opens[j]:  # bearish candle
                    ob_top = highs[j]
                    ob_bot = lows[j]
                    active_bull_obs.append((ob_top, ob_bot))
                    break

        # Bearish OB: find last bullish candle before bearish displacement
        if is_bearish_displacement:
            for j in range(i - 1, max(i - swing_len, 0) - 1, -1):
                if closes[j] > opens[j]:  # bullish candle
                    ob_top = highs[j]
                    ob_bot = lows[j]
                    active_bear_obs.append((ob_top, ob_bot))
                    break

        # Mitigate (remove) order blocks that price has passed through
        active_bull_obs = [(t, b) for t, b in active_bull_obs
                          if lows[i] <= t]  # still above or touching OB
        active_bear_obs = [(t, b) for t, b in active_bear_obs
                          if highs[i] >= b]  # still below or touching OB

        # Record nearest unmitigated OB
        if active_bull_obs:
            # Nearest bullish OB = closest one above current low
            nearest = min(active_bull_obs, key=lambda x: abs(closes[i] - x[0]))
            ob_bull_top[i] = nearest[0]
            ob_bull_bot[i] = nearest[1]

        if active_bear_obs:
            nearest = min(active_bear_obs, key=lambda x: abs(closes[i] - x[0]))
            ob_bear_top[i] = nearest[0]
            ob_bear_bot[i] = nearest[1]

    df["ob_bull_top"] = ob_bull_top
    df["ob_bull_bot"] = ob_bull_bot
    df["ob_bear_top"] = ob_bear_top
    df["ob_bear_bot"] = ob_bear_bot

    return df


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 10) -> pd.DataFrame:
    """
    Detect liquidity sweeps — price wicks beyond recent swing highs/lows
    then closes back inside, indicating a stop hunt / liquidity grab.

    Bullish sweep: wick below recent swing low, close back above it
    Bearish sweep: wick above recent swing high, close back below it

    Adds columns:
      liq_sweep_bull: 1.0 if bullish liquidity sweep on this bar
      liq_sweep_bear: 1.0 if bearish liquidity sweep on this bar
    """
    n = len(df)
    sweep_bull = np.zeros(n)
    sweep_bear = np.zeros(n)

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values

    for i in range(lookback + 1, n):
        # Find recent swing low (lowest low in lookback, excluding current)
        recent_lows = lows[i - lookback:i]
        swing_low = np.min(recent_lows)
        # Find recent swing high
        recent_highs = highs[i - lookback:i]
        swing_high = np.max(recent_highs)

        # Bullish sweep: wick goes below swing low but closes above it
        # AND candle closes bullish (or at least above the sweep level)
        if lows[i] < swing_low and closes[i] > swing_low:
            if closes[i] > opens[i]:  # bullish close
                sweep_bull[i] = 1.0

        # Bearish sweep: wick goes above swing high but closes below it
        if highs[i] > swing_high and closes[i] < swing_high:
            if closes[i] < opens[i]:  # bearish close
                sweep_bear[i] = 1.0

    df["liq_sweep_bull"] = sweep_bull
    df["liq_sweep_bear"] = sweep_bear

    return df


def has_recent_fvg(df: pd.DataFrame, idx: int, direction: int,
                   lookback: int = 10) -> bool:
    """
    Check if there's a recent FVG in the given direction within lookback bars.
    direction: 1 = bullish, -1 = bearish
    """
    start = max(0, idx - lookback)
    if direction == 1:
        col = "fvg_bull"
    else:
        col = "fvg_bear"

    if col not in df.columns:
        return False

    return df[col].iloc[start:idx + 1].sum() > 0


def has_recent_liquidity_sweep(df: pd.DataFrame, idx: int, direction: int,
                               lookback: int = 10) -> bool:
    """
    Check if there was a liquidity sweep in the given direction recently.
    direction: 1 = bullish sweep (swept lows), -1 = bearish sweep (swept highs)
    """
    start = max(0, idx - lookback)
    if direction == 1:
        col = "liq_sweep_bull"
    else:
        col = "liq_sweep_bear"

    if col not in df.columns:
        return False

    return df[col].iloc[start:idx + 1].sum() > 0


def get_nearest_ob_sl(df: pd.DataFrame, idx: int, direction: int,
                      entry: float, max_sl_pips: float = 50) -> float:
    """
    Get SL from nearest Order Block. Returns 0 if no suitable OB found.
    direction: 1 = buy (SL below OB bottom), -1 = sell (SL above OB top)
    """
    if direction == 1 and "ob_bull_bot" in df.columns:
        ob_bot = df["ob_bull_bot"].iloc[idx]
        if not np.isnan(ob_bot) and ob_bot < entry:
            sl_dist = price_to_pips(entry - ob_bot)
            if 10 <= sl_dist <= max_sl_pips:
                return ob_bot
    elif direction == -1 and "ob_bear_top" in df.columns:
        ob_top = df["ob_bear_top"].iloc[idx]
        if not np.isnan(ob_top) and ob_top > entry:
            sl_dist = price_to_pips(ob_top - entry)
            if 10 <= sl_dist <= max_sl_pips:
                return ob_top
    return 0.0


def calculate_smc(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all SMC indicators on the DataFrame."""
    df = detect_fvg(df)
    df = detect_order_blocks(df)
    df = detect_liquidity_sweep(df)
    return df
