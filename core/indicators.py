"""
core/indicators.py - Technical indicator calculations.
Pure pandas/pandas_ta, platform-independent.
"""
import pandas as pd
import pandas_ta as ta


def calculate_all(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Calculate all indicators on OHLCV DataFrame.
    Expects columns: open, high, low, close, volume (lowercase).
    Returns df with indicator columns added.
    """
    s = cfg["strategy"]

    # --- EMAs ---
    df["ema_fast"] = ta.ema(df["close"], length=s["ema_fast"])

    # For H4 frame we also calculate slow/trend EMAs
    if "ema_slow" not in df.columns:
        df["ema_slow"] = ta.ema(df["close"], length=s["ema_slow"])
    if "ema_trend" not in df.columns:
        df["ema_trend"] = ta.ema(df["close"], length=s["ema_trend"])

    # --- RSI ---
    df["rsi"] = ta.rsi(df["close"], length=s["rsi_period"])

    # --- ATR ---
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=s["atr_period"])

    # --- ADX with +DI / -DI ---
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=s["adx_period"])
    if adx_df is not None and len(adx_df.columns) >= 3:
        df["adx"] = adx_df.iloc[:, 0]
        df["plus_di"] = adx_df.iloc[:, 1]
        df["minus_di"] = adx_df.iloc[:, 2]
    else:
        df["adx"] = 0.0
        df["plus_di"] = 0.0
        df["minus_di"] = 0.0

    return df


def calculate_h4_trend(h4_df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Calculate trend indicators on H4 data."""
    s = cfg["strategy"]
    h4_df["ema_slow"] = ta.ema(h4_df["close"], length=s["ema_slow"])
    h4_df["ema_trend"] = ta.ema(h4_df["close"], length=s["ema_trend"])

    adx_df = ta.adx(h4_df["high"], h4_df["low"], h4_df["close"], length=s["adx_period"])
    if adx_df is not None and len(adx_df.columns) >= 3:
        h4_df["adx"] = adx_df.iloc[:, 0]
        h4_df["plus_di"] = adx_df.iloc[:, 1]
        h4_df["minus_di"] = adx_df.iloc[:, 2]
    else:
        h4_df["adx"] = 0.0
        h4_df["plus_di"] = 0.0
        h4_df["minus_di"] = 0.0

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
