"""
broker/data_feed.py - Price data abstraction.

  dry_run / backtest: uses yfinance (works on Mac)
  live:               uses MT5 copy_rates (Windows VPS)
"""
from __future__ import annotations
import time
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional
from core.indicators import resample_to_h4


class DataFeed:
    def __init__(self, cfg: dict, gateway=None):
        self.cfg = cfg
        self.mode = cfg.get("mode", "dry_run")
        self.symbol = cfg["symbol"]
        self.gateway = gateway

    # ------------------------------------------------------------------
    def get_h1(self, bars: int = 300) -> Optional[pd.DataFrame]:
        """Get H1 OHLCV data. Returns DataFrame with lowercase columns."""
        if self.mode == "live":
            return self._get_mt5_data("H1", bars)
        return self._get_yfinance_data("1h", bars)

    # ------------------------------------------------------------------
    def get_h4(self, bars: int = 200) -> Optional[pd.DataFrame]:
        """Get H4 OHLCV data."""
        if self.mode == "live":
            return self._get_mt5_data("H4", bars)

        # yfinance doesn't support 4h directly; resample from H1
        h1 = self._get_yfinance_data("1h", bars * 4 + 50)
        if h1 is None or len(h1) < 50:
            return None
        h4 = resample_to_h4(h1)
        return h4.tail(bars) if len(h4) > bars else h4

    # ------------------------------------------------------------------
    def get_historical(self, timeframe: str = "1h",
                       start: str = "2020-01-01",
                       end: str = "2025-12-31") -> Optional[pd.DataFrame]:
        """
        Get full historical data for backtesting.
        yfinance limits H1 to ~730 days from today.
        Downloads in 200-day chunks, auto-adjusting start to the
        earliest date yfinance can serve.
        """
        try:
            import yfinance as yf
            ticker = self._yf_ticker()

            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")
            now = datetime.now()

            # yfinance H1 limit: start must be within ~729 days of today
            if timeframe in ("1h", "60m"):
                earliest_valid = now - timedelta(days=729)
                if start_dt < earliest_valid:
                    print(f"[DATA] Adjusting start from {start_dt.date()} to "
                          f"{earliest_valid.date()} (yfinance 730-day H1 limit)")
                    start_dt = earliest_valid

            # Cap end date to tomorrow
            if end_dt > now + timedelta(days=1):
                end_dt = now + timedelta(days=1)

            # Use 200-day chunks to stay well within limits
            chunk_days = 200

            all_chunks = []
            current_start = start_dt

            while current_start < end_dt:
                current_end = min(current_start + timedelta(days=chunk_days), end_dt)
                print(f"[DATA] Downloading {current_start.date()} → {current_end.date()}...")

                chunk = yf.download(
                    ticker,
                    start=current_start.strftime("%Y-%m-%d"),
                    end=current_end.strftime("%Y-%m-%d"),
                    interval=timeframe,
                    progress=False,
                    auto_adjust=True,
                )
                if chunk is not None and not chunk.empty:
                    normalized = self._normalize_yf(chunk)
                    if not normalized.empty:
                        all_chunks.append(normalized)
                        print(f"[DATA]   → Got {len(normalized)} bars")
                    else:
                        print(f"[DATA]   → Empty after normalization")
                else:
                    print(f"[DATA]   → No data returned")

                current_start = current_end
                time.sleep(0.5)  # Rate limit courtesy

            if not all_chunks:
                print("[DATA] No data downloaded")
                return None

            df = pd.concat(all_chunks)
            df = df[~df.index.duplicated(keep="first")]
            df.sort_index(inplace=True)
            print(f"[DATA] Total: {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}")
            return df

        except Exception as e:
            print(f"[DATA] Historical download failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    # ------------------------------------------------------------------
    #  Private: yfinance
    # ------------------------------------------------------------------
    def _get_yfinance_data(self, interval: str, bars: int) -> Optional[pd.DataFrame]:
        try:
            import yfinance as yf

            ticker = self._yf_ticker()
            # yfinance max periods for intraday
            if interval == "1h":
                period = "730d"  # max for 1h
            elif interval == "1d":
                period = "5y"
            else:
                period = "60d"

            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None

            df = self._normalize_yf(df)
            return df.tail(bars) if len(df) > bars else df

        except Exception as e:
            print(f"[DATA] yfinance error: {e}")
            return None

    def _yf_ticker(self) -> str:
        # yfinance forex tickers
        if self.symbol.upper() in ("EURUSD", "EURUSD."):
            return "EURUSD=X"
        return f"{self.symbol}=X"

    @staticmethod
    def _normalize_yf(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize yfinance DataFrame to standard format."""
        df = df.copy()
        # Handle multi-level columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        # Ensure required columns
        required = ["open", "high", "low", "close"]
        for col in required:
            if col not in df.columns:
                return pd.DataFrame()

        if "volume" not in df.columns:
            df["volume"] = 0

        # Make timezone-aware (UTC)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        return df.dropna(subset=required)

    # ------------------------------------------------------------------
    #  Private: MT5
    # ------------------------------------------------------------------
    def _get_mt5_data(self, timeframe: str, bars: int) -> Optional[pd.DataFrame]:
        try:
            import MetaTrader5 as mt5

            tf_map = {
                "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1,
            }
            mt5_tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)

            rates = mt5.copy_rates_from_pos(self.symbol, mt5_tf, 0, bars)
            if rates is None or len(rates) == 0:
                return None

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df.set_index("time", inplace=True)
            df.rename(columns={
                "open": "open", "high": "high", "low": "low",
                "close": "close", "tick_volume": "volume"
            }, inplace=True)

            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            print(f"[DATA] MT5 data error: {e}")
            return None
