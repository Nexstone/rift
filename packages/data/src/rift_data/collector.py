"""Persistent data collector daemon.

Runs continuously in the background, collecting:
1. Candle data (closes) for configured pairs and timeframes
2. Funding rates (hourly)
3. L2 order book snapshots (every 1-5 minutes)
4. Market metadata (mid prices, open interest)

Data is saved to local Parquet files and SQLite database.
The longer it runs, the more valuable the data becomes —
order book snapshots cannot be retrieved retroactively.

Usage:
    rift collect start       # start the daemon
    rift collect stop        # stop the daemon
    rift collect status      # check what's being collected
"""

from __future__ import annotations

import json
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from rift_data.data import get_info_client, save_candles, save_funding_rates, fetch_candles, fetch_funding_rates

COLLECTOR_DB = Path.home() / ".rift" / "collector.db"
COLLECTOR_PID = Path.home() / ".rift" / "collector.pid"
COLLECTOR_LOG = Path.home() / ".rift" / "collector.log"
DATA_DIR = Path.home() / ".rift" / "data"


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize the collector SQLite database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            bids TEXT NOT NULL,
            asks TEXT NOT NULL,
            bid_volume REAL NOT NULL,
            ask_volume REAL NOT NULL,
            imbalance REAL NOT NULL,
            mid_price REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            mid_price REAL NOT NULL,
            funding_rate REAL,
            open_interest REAL
        );

        CREATE TABLE IF NOT EXISTS collector_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            candles_collected INTEGER DEFAULT 0,
            funding_collected INTEGER DEFAULT 0,
            orderbook_snapshots INTEGER DEFAULT 0,
            market_snapshots INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_ob_ts ON orderbook_snapshots(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ob_symbol ON orderbook_snapshots(symbol);
        CREATE INDEX IF NOT EXISTS idx_market_ts ON market_snapshots(timestamp);
        CREATE INDEX IF NOT EXISTS idx_market_symbol ON market_snapshots(symbol);
    """)

    conn.commit()
    return conn


def _emit(data: dict) -> None:
    """Write NDJSON to stdout."""
    print(json.dumps(data), flush=True)


def _log(msg: str, log_path: Path = COLLECTOR_LOG) -> None:
    """Append to the collector log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a") as f:
        f.write(f"[{timestamp}] {msg}\n")


def collect_orderbook(info, symbol: str, conn: sqlite3.Connection) -> dict | None:
    """Snapshot the L2 order book for a symbol."""
    try:
        book = info.l2_snapshot(symbol)
        if not book or "levels" not in book:
            return None

        levels = book["levels"]
        if len(levels) < 2:
            return None

        bids = levels[0]  # [[price, size], ...]
        asks = levels[1]

        bid_volume = sum(float(b["sz"]) for b in bids) if bids else 0
        ask_volume = sum(float(a["sz"]) for a in asks) if asks else 0
        total_volume = bid_volume + ask_volume

        imbalance = (bid_volume - ask_volume) / total_volume if total_volume > 0 else 0

        best_bid = float(bids[0]["px"]) if bids else 0
        best_ask = float(asks[0]["px"]) if asks else 0
        mid_price = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0

        ts = int(time.time() * 1000)

        conn.execute(
            "INSERT INTO orderbook_snapshots (timestamp, symbol, bids, asks, bid_volume, ask_volume, imbalance, mid_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, symbol, json.dumps(bids[:20]), json.dumps(asks[:20]), bid_volume, ask_volume, round(imbalance, 6), mid_price),
        )

        return {
            "symbol": symbol,
            "mid_price": mid_price,
            "imbalance": round(imbalance, 4),
            "bid_volume": round(bid_volume, 2),
            "ask_volume": round(ask_volume, 2),
        }

    except Exception as e:
        _log(f"Orderbook error for {symbol}: {e}")
        return None


def collect_market_data(info, symbol: str, conn: sqlite3.Connection) -> dict | None:
    """Collect mid price, funding rate, and open interest."""
    try:
        # Get mid prices
        mids = info.all_mids()
        mid_price = float(mids.get(symbol, 0))

        # Get funding rate. NULL on fetch failure — never persist a fake 0.0
        # that downstream backtests would treat as a real reading.
        funding_rate: float | None = None
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (2 * 60 * 60 * 1000)
            funding = info.funding_history(symbol, start_time, end_time)
            if funding:
                funding_rate = float(funding[-1]["fundingRate"])
        except Exception as e:
            _log(f"Funding history error for {symbol}: {e}")

        ts = int(time.time() * 1000)

        conn.execute(
            "INSERT INTO market_snapshots (timestamp, symbol, mid_price, funding_rate, open_interest) VALUES (?, ?, ?, ?, ?)",
            (ts, symbol, mid_price, funding_rate, None),
        )

        return {
            "symbol": symbol,
            "mid_price": mid_price,
            "funding_rate": funding_rate,
        }

    except Exception as e:
        _log(f"Market data error for {symbol}: {e}")
        return None


def collect_candles_and_funding(info, symbols: list[str], timeframes: list[str]) -> dict:
    """Fetch latest candles and funding rates for all pairs."""
    import polars as pl

    results = {"candles": 0, "funding": 0}

    for symbol in symbols:
        # Candles
        for tf in timeframes:
            try:
                end_time = int(time.time() * 1000)
                start_time = end_time - (6 * 60 * 60 * 1000)  # last 6 hours
                candles = info.candles_snapshot(symbol, tf, start_time, end_time)
                if candles:
                    df = pl.DataFrame({
                        "timestamp": [c["t"] for c in candles],
                        "open": [float(c["o"]) for c in candles],
                        "high": [float(c["h"]) for c in candles],
                        "low": [float(c["l"]) for c in candles],
                        "close": [float(c["c"]) for c in candles],
                        "volume": [float(c["v"]) for c in candles],
                        "num_trades": [c["n"] for c in candles],
                    })
                    df = df.unique(subset=["timestamp"]).sort("timestamp")
                    save_candles(df, symbol, tf)
                    results["candles"] += len(candles)
            except Exception as e:
                _log(f"Candle error {symbol} {tf}: {e}")

        # Funding rates
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (2 * 60 * 60 * 1000)
            funding = info.funding_history(symbol, start_time, end_time)
            if funding:
                df = pl.DataFrame({
                    "timestamp": [f["time"] for f in funding],
                    "funding_rate": [float(f["fundingRate"]) for f in funding],
                    "premium": [float(f["premium"]) for f in funding],
                })
                df = df.unique(subset=["timestamp"]).sort("timestamp")
                save_funding_rates(df, symbol)
                results["funding"] += len(funding)
        except Exception as e:
            _log(f"Funding error {symbol}: {e}")

    return results


def run_collector(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    orderbook_interval: int = 60,  # seconds between orderbook snapshots
    candle_interval: int = 300,     # seconds between candle fetches (5 min)
    market_interval: int = 60,      # seconds between market snapshots
) -> None:
    """Run the data collector. Runs until interrupted."""

    if symbols is None:
        symbols = ["BTC", "ETH", "SOL", "HYPE"]
    if timeframes is None:
        # Frequency-agnostic default — common TFs from minute to daily.
        # Narrow via --tf when only a subset is needed.
        timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]

    # Write PID file
    COLLECTOR_PID.parent.mkdir(parents=True, exist_ok=True)
    COLLECTOR_PID.write_text(str(sys.modules['os'].getpid()) if 'os' in sys.modules else str(0))

    import os
    COLLECTOR_PID.write_text(str(os.getpid()))

    info = get_info_client()
    conn = _init_db(COLLECTOR_DB)

    running = True
    stats = {"candles": 0, "funding": 0, "orderbook": 0, "market": 0, "errors": 0}

    def handle_shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    _log(f"Collector started: symbols={symbols}, timeframes={timeframes}")
    _emit({"type": "status", "msg": f"Collector started: {', '.join(symbols)} | Timeframes: {', '.join(timeframes)}"})

    last_candle_fetch = 0
    last_orderbook_fetch = 0
    last_market_fetch = 0
    tick = 0

    while running:
        try:
            now = time.time()

            # Order book snapshots (every orderbook_interval seconds)
            if now - last_orderbook_fetch >= orderbook_interval:
                for symbol in symbols:
                    result = collect_orderbook(info, symbol, conn)
                    if result:
                        stats["orderbook"] += 1
                conn.commit()
                last_orderbook_fetch = now

            # Market data snapshots (every market_interval seconds)
            if now - last_market_fetch >= market_interval:
                for symbol in symbols:
                    result = collect_market_data(info, symbol, conn)
                    if result:
                        stats["market"] += 1
                conn.commit()
                last_market_fetch = now

            # Candle + funding fetch (every candle_interval seconds)
            if now - last_candle_fetch >= candle_interval:
                cf_result = collect_candles_and_funding(info, symbols, timeframes)
                stats["candles"] += cf_result["candles"]
                stats["funding"] += cf_result["funding"]
                last_candle_fetch = now

            # Periodic status emit (every 5 minutes)
            tick += 1
            if tick % 5 == 0:
                uptime_min = int(tick)
                _emit({
                    "type": "heartbeat",
                    "uptime_minutes": uptime_min,
                    "stats": stats,
                    "db_size_mb": round(COLLECTOR_DB.stat().st_size / (1024 * 1024), 2) if COLLECTOR_DB.exists() else 0,
                })

                # Log stats periodically
                ts = int(time.time() * 1000)
                conn.execute(
                    "INSERT INTO collector_stats (timestamp, candles_collected, funding_collected, orderbook_snapshots, market_snapshots, errors) VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, stats["candles"], stats["funding"], stats["orderbook"], stats["market"], stats["errors"]),
                )
                conn.commit()

        except Exception as e:
            stats["errors"] += 1
            _log(f"Collector error: {e}")

        time.sleep(min(orderbook_interval, market_interval, candle_interval))

    # Shutdown
    conn.close()
    if COLLECTOR_PID.exists():
        COLLECTOR_PID.unlink()
    _log(f"Collector stopped. Stats: {stats}")
    _emit({"type": "shutdown", "msg": "Collector stopped", "stats": stats})


def get_collector_stats() -> dict:
    """Get collector status and statistics."""
    result = {
        "running": COLLECTOR_PID.exists(),
        "pid": None,
        "db_exists": COLLECTOR_DB.exists(),
        "db_size_mb": 0,
        "orderbook_count": 0,
        "market_count": 0,
        "oldest_data": None,
        "newest_data": None,
        "symbols": [],
    }

    if COLLECTOR_PID.exists():
        try:
            pid = int(COLLECTOR_PID.read_text().strip())
            result["pid"] = pid
            # Check if process actually exists
            import os
            try:
                os.kill(pid, 0)
            except OSError:
                result["running"] = False
        except Exception:
            result["running"] = False

    if COLLECTOR_DB.exists():
        result["db_size_mb"] = round(COLLECTOR_DB.stat().st_size / (1024 * 1024), 2)

        try:
            conn = sqlite3.connect(str(COLLECTOR_DB))

            # Count records
            result["orderbook_count"] = conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0]
            result["market_count"] = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]

            # Date range
            row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM orderbook_snapshots").fetchone()
            if row[0]:
                result["oldest_data"] = datetime.fromtimestamp(row[0] / 1000).strftime("%Y-%m-%d %H:%M")
                result["newest_data"] = datetime.fromtimestamp(row[1] / 1000).strftime("%Y-%m-%d %H:%M")

            # Symbols
            symbols = conn.execute("SELECT DISTINCT symbol FROM orderbook_snapshots").fetchall()
            result["symbols"] = [s[0] for s in symbols]

            conn.close()
        except Exception:
            pass

    return result
