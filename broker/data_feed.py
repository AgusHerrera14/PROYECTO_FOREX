"""
broker/data_feed.py - Price data abstraction.

Data source priority for backtesting:
  1. CSV files in data/ folder (platform-independent, exported via data_export.py)
  2. MT5 copy_rates_range (Windows only, 10+ years of H1 data)
  3. yfinance (any platform, limited to ~730 days for H1)

Real-time (dry_run / live):
  dry_run: yfinance
  live:    MT5 copy_rates
"""
from __future__ import annotations
import time
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
        Priority: 1) CSV in data/ → 2) MT5 → 3) yfinance (730-day limit)
        """
        # --- 1. Try CSV files first (exported via data_export.py) ---
        csv_df = self._load_csv_historical(timeframe, start, end)
        if csv_df is not None:
            return csv_df

        # --- 2. Try MT5 (Windows only, 10+ years) ---
        mt5_df = self._get_mt5_historical(timeframe, start, end)
        if mt5_df is not None:
            return mt5_df

        # --- 3. Fallback: yfinance (limited to ~730 days for H1) ---
        return self._get_yfinance_historical(timeframe, start, end)

    # ------------------------------------------------------------------
    def _load_csv_historical(self, timeframe: str, start: str,
                             end: str) -> Optional[pd.DataFrame]:
        """Load historical data from CSV files in data/ folder."""
        data_dir = Path("data")
        if not data_dir.exists():
            return None

        # Look for matching CSV: data/EURUSD_H1.csv
        tf_label = timeframe.upper().replace("1H", "H1").replace("4H", "H4")
        csv_path = data_dir / f"{self.symbol}_{tf_label}.csv"

        if not csv_path.exists():
            return None

        try:
            print(f"[DATA] Loading from CSV: {csv_path}")
            df = pd.read_csv(csv_path, parse_dates=["time"], index_col="time")
            df.columns = [c.lower() for c in df.columns]

            # Ensure UTC timezone
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")

            # Filter date range
            start_dt = pd.Timestamp(start, tz="UTC")
            end_dt = pd.Timestamp(end, tz="UTC")
            df = df[(df.index >= start_dt) & (df.index <= end_dt)]

            if len(df) < 50:
                print(f"[DATA] CSV has only {len(df)} bars in range, skipping")
                return None

            print(f"[DATA] CSV loaded: {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}")
            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            print(f"[DATA] CSV load error: {e}")
            return None

    # ------------------------------------------------------------------
    def _get_mt5_historical(self, timeframe: str, start: str,
                            end: str) -> Optional[pd.DataFrame]:
        """Fetch historical data from MT5 using copy_rates_range."""
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return None

        if not mt5.terminal_info():
            # MT5 not initialized — try to init
            mt5_cfg = self.cfg.get("mt5", {})
            if not mt5.initialize(path=mt5_cfg.get("path", ""),
                                  timeout=mt5_cfg.get("timeout", 10000)):
                return None

        try:
            tf_map = {
                "1h": mt5.TIMEFRAME_H1, "H1": mt5.TIMEFRAME_H1,
                "4h": mt5.TIMEFRAME_H4, "H4": mt5.TIMEFRAME_H4,
                "1d": mt5.TIMEFRAME_D1, "D1": mt5.TIMEFRAME_D1,
            }
            mt5_tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)

            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")

            print(f"[DATA] Fetching from MT5: {self.symbol} {timeframe} "
                  f"{start_dt.date()} → {end_dt.date()}...")

            rates = mt5.copy_rates_range(self.symbol, mt5_tf, start_dt, end_dt)
            if rates is None or len(rates) == 0:
                print(f"[DATA] MT5 returned no data: {mt5.last_error()}")
                return None

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df.set_index("time", inplace=True)
            df.rename(columns={"tick_volume": "volume"}, inplace=True)
            df = df[["open", "high", "low", "close", "volume"]]

            print(f"[DATA] MT5 loaded: {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}")
            return df

        except Exception as e:
            print(f"[DATA] MT5 historical error: {e}")
            return None

    # ------------------------------------------------------------------
    def _get_yfinance_historical(self, timeframe: str, start: str,
                                 end: str) -> Optional[pd.DataFrame]:
        """Fallback: download from yfinance (H1 limited to ~730 days)."""
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
                time.sleep(0.5)

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
