"""
core/pandas_ta_shim.py - Pure numpy/pandas replacement for pandas_ta.

Implements only the 4 functions used by indicators.py:
  ema(close, length) -> pd.Series
  rsi(close, length) -> pd.Series
  atr(high, low, close, length) -> pd.Series
  adx(high, low, close, length) -> pd.DataFrame  (columns: ADX, +DI, -DI)

Drop-in replacement: `import core.pandas_ta_shim as ta` works the same
as `import pandas_ta as ta` for our use case.
"""
import numpy as np
import pandas as pd


def ema(close: pd.Series, length: int = 10) -> pd.Series:
    """Exponential Moving Average."""
    return close.ewm(span=length, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    return result


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        length: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        length: int = 14) -> pd.DataFrame:
    """
    Average Directional Index with +DI and -DI.
    Returns DataFrame with columns: ADX_{length}, DMP_{length}, DMN_{length}
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    plus_dm = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)

    minus_dm = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)

    atr_val = atr(high, low, close, length)

    smooth_plus_dm = plus_dm.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    smooth_minus_dm = minus_dm.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    plus_di = 100.0 * smooth_plus_dm / atr_val.replace(0, np.nan)
    minus_di = 100.0 * smooth_minus_dm / atr_val.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    result = pd.DataFrame({
        f"ADX_{length}": adx_val,
        f"DMP_{length}": plus_di,
        f"DMN_{length}": minus_di,
    }, index=close.index)

    return result
