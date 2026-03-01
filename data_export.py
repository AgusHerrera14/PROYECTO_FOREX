#!/usr/bin/env python3
"""
data_export.py - Export historical data to CSV for backtesting.

Two sources:
  1. MT5 (run on Windows laptop with MT5 open)
  2. Dukascopy (run anywhere, free, data from 2003+)

Usage:
  python data_export.py --source mt5                         # Export from MT5
  python data_export.py --source dukascopy                   # Download from Dukascopy
  python data_export.py --source dukascopy --start 2015-01-01  # Custom start date

Output: data/EURUSD_H1.csv
"""
from __future__ import annotations
import argparse
import io
import struct
import lzma
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests


DATA_DIR = Path("data")


# =====================================================================
# MT5 EXPORT
# =====================================================================
def export_mt5(symbol: str, start: str, end: str):
    """Export H1 data from MT5 to CSV. Requires Windows + MT5 terminal open."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("ERROR: MetaTrader5 package not installed.")
        print("Run: pip install MetaTrader5")
        print("NOTE: Only works on Windows with MT5 terminal installed.")
        sys.exit(1)

    if not mt5.initialize():
        print(f"MT5 initialize failed: {mt5.last_error()}")
        print("Make sure MT5 terminal is open and logged in.")
        sys.exit(1)

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")

    print(f"Fetching {symbol} H1 from MT5: {start} → {end}...")
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, start_dt, end_dt)

    if rates is None or len(rates) == 0:
        print(f"No data returned: {mt5.last_error()}")
        mt5.shutdown()
        sys.exit(1)

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["time", "open", "high", "low", "close", "tick_volume"]]
    df.rename(columns={"tick_volume": "volume"}, inplace=True)

    DATA_DIR.mkdir(exist_ok=True)
    csv_path = DATA_DIR / f"{symbol}_H1.csv"
    df.to_csv(csv_path, index=False)

    print(f"Exported {len(df)} bars to {csv_path}")
    print(f"Date range: {df['time'].iloc[0]} → {df['time'].iloc[-1]}")

    mt5.shutdown()


# =====================================================================
# DUKASCOPY DOWNLOAD
# =====================================================================
# Dukascopy stores tick data in bi5 (LZMA-compressed binary) files.
# URL pattern: datafeed.dukascopy.com/datafeed/EURUSD/YYYY/MM/DD/HHh_ticks.bi5
# We download hourly tick files and aggregate to H1 OHLCV bars.

DUKASCOPY_URL = "https://datafeed.dukascopy.com/datafeed"

# Pip size for price decoding (Dukascopy stores prices as integers)
DUKASCOPY_POINT = {
    "EURUSD": 1e-5, "GBPUSD": 1e-5, "USDCHF": 1e-5, "AUDUSD": 1e-5,
    "USDJPY": 1e-3, "GBPJPY": 1e-3, "EURJPY": 1e-3,
}


def _download_hour(symbol: str, dt: datetime, session: requests.Session,
                   max_retries: int = 3) -> list:
    """Download one hour of tick data from Dukascopy. Returns list of (time, ask, bid, vol)."""
    # Dukascopy months are 0-indexed
    month_idx = dt.month - 1
    url = (f"{DUKASCOPY_URL}/{symbol}/"
           f"{dt.year:04d}/{month_idx:02d}/{dt.day:02d}/"
           f"{dt.hour:02d}h_ticks.bi5")

    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code != 200 or len(resp.content) == 0:
                return []

            # Decompress LZMA
            raw = lzma.decompress(resp.content)
            if len(raw) == 0:
                return []

            # Each tick = 20 bytes: uint32 ms_offset, uint32 ask, uint32 bid,
            #                        float32 ask_vol, float32 bid_vol
            n_ticks = len(raw) // 20
            point = DUKASCOPY_POINT.get(symbol, 1e-5)
            ticks = []
            for i in range(n_ticks):
                chunk = raw[i * 20:(i + 1) * 20]
                ms, ask_i, bid_i, ask_v, bid_v = struct.unpack(">IIIff", chunk)
                tick_time = dt + timedelta(milliseconds=ms)
                ask = ask_i * point
                bid = bid_i * point
                vol = ask_v + bid_v
                ticks.append((tick_time, ask, bid, vol))
            return ticks

        except (lzma.LZMAError, struct.error):
            return []
        except requests.RequestException:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s
                continue
            return []
    return []


def _ticks_to_h1_bar(ticks: list, bar_time: datetime) -> dict | None:
    """Aggregate ticks into a single H1 OHLCV bar using mid price."""
    if not ticks:
        return None

    mids = [(t[1] + t[2]) / 2 for t in ticks]  # (ask+bid)/2
    vols = [t[3] for t in ticks]

    return {
        "time": bar_time,
        "open": mids[0],
        "high": max(mids),
        "low": min(mids),
        "close": mids[-1],
        "volume": sum(vols),
    }


def _build_hour_list(start_dt: datetime, end_dt: datetime) -> list[datetime]:
    """Build list of hours to download, skipping weekends."""
    hours = []
    current = start_dt
    while current < end_dt:
        # Skip weekends (Sat 22:00 UTC to Sun 22:00 UTC)
        if current.weekday() == 5 and current.hour >= 22:
            current += timedelta(hours=1)
            continue
        if current.weekday() == 6:
            current += timedelta(hours=1)
            continue
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def _download_batch(args):
    """Download a single hour — for use with ThreadPoolExecutor."""
    symbol, dt, max_retries = args
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    ticks = _download_hour(symbol, dt, session, max_retries)
    if ticks:
        bar = _ticks_to_h1_bar(ticks, dt)
        return bar
    return None


def download_dukascopy(symbol: str, start: str, end: str, workers: int = 10):
    """Download H1 data from Dukascopy using parallel connections and save to CSV."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    hours = _build_hour_list(start_dt, end_dt)
    total = len(hours)
    print(f"Downloading {symbol} from Dukascopy: {start} → {end}")
    print(f"Market hours to fetch: {total:,} | Parallel workers: {workers}")
    print()

    bars = []
    errors = 0
    done = 0
    last_print = time.time()

    # Process in daily chunks (24 hours) to show progress and save incrementally
    chunk_size = 24 * workers  # ~240 hours per batch
    for chunk_start in range(0, total, chunk_size):
        chunk = hours[chunk_start:chunk_start + chunk_size]
        tasks = [(symbol, dt, 3) for dt in chunk]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_batch, t): t for t in tasks}
            for future in as_completed(futures):
                done += 1
                result = future.result()
                if result:
                    bars.append(result)
                else:
                    errors += 1

                if time.time() - last_print > 10:
                    pct = done / max(total, 1) * 100
                    print(f"  Progress: {pct:.1f}% | {len(bars):,} bars | "
                          f"Done: {done:,}/{total:,} | "
                          f"Empty hours: {errors}")
                    last_print = time.time()

    if not bars:
        print("ERROR: No data downloaded")
        sys.exit(1)

    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df.sort_values("time", inplace=True)
    df.drop_duplicates(subset=["time"], keep="first", inplace=True)

    DATA_DIR.mkdir(exist_ok=True)
    csv_path = DATA_DIR / f"{symbol}_H1.csv"
    df.to_csv(csv_path, index=False)

    print()
    print(f"Done! Exported {len(df):,} H1 bars to {csv_path}")
    print(f"Date range: {df['time'].iloc[0]} → {df['time'].iloc[-1]}")
    print(f"File size: {csv_path.stat().st_size / 1024 / 1024:.1f} MB")


# =====================================================================
# CLI
# =====================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Export historical EURUSD H1 data for backtesting")
    parser.add_argument("--source", choices=["mt5", "dukascopy"],
                        default="dukascopy",
                        help="Data source (default: dukascopy)")
    parser.add_argument("--symbol", default="EURUSD", help="Symbol")
    parser.add_argument("--start", default="2018-01-01",
                        help="Start date YYYY-MM-DD (default: 2018-01-01)")
    parser.add_argument("--end", default="2026-03-01",
                        help="End date YYYY-MM-DD (default: 2026-03-01)")
    args = parser.parse_args()

    print(f"\n📊 Data Export | Source: {args.source.upper()} | "
          f"{args.symbol} H1\n")

    if args.source == "mt5":
        export_mt5(args.symbol, args.start, args.end)
    else:
        download_dukascopy(args.symbol, args.start, args.end)


if __name__ == "__main__":
    main()
