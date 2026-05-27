"""Hyperliquid REST API fetching and local cache I/O.

Schema helpers (normalize_coin, coin_to_path, etc.) live in rift_core.
S3 archive sync lives in rift_data.s3. This module covers:

- Hyperliquid REST API: candles, funding, market context, OI, borrow rates
- Local parquet I/O: save_candles, save_funding_rates, load_candles, load_funding_rates
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import polars as pl
from hyperliquid.info import Info
from hyperliquid.utils import constants

from rift_core.schema import (
    VALID_INTERVALS,
    coin_to_path,
    normalize_coin,
    path_to_coin,
)

DEFAULT_DATA_DIR = Path.home() / ".rift" / "data"


def get_info_client() -> Info:
    """Create a Hyperliquid Info client with optional proxy support."""
    from rift_core.config import get_proxy

    base_url = constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)

    # Prevent infinite hangs — 30 second timeout on all API calls
    info.session.timeout = 30

    # Inject proxy into the SDK's requests session
    proxy = get_proxy()
    if proxy:
        info.session.proxies = {"http": proxy, "https": proxy}

    return info


def _is_hip3(coin: str) -> bool:
    """Check if coin is a HIP-3 (TradFi) ticker."""
    return coin.startswith("xyz:")


def _raw_candles_snapshot(coin: str, interval: str, start_time: int, end_time: int) -> list[dict]:
    """Fetch candles via raw HTTP for HIP-3 coins (SDK doesn't support xyz: prefix)."""
    import requests
    from rift_core.config import get_proxy

    url = constants.MAINNET_API_URL
    url = url.rstrip("/") + "/info"
    proxies = {}
    proxy = get_proxy()
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    resp = requests.post(url, json={
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_time, "endTime": end_time},
    }, proxies=proxies, timeout=30)
    data = resp.json()
    return data if isinstance(data, list) else []


def _raw_funding_history(coin: str, start_time: int, end_time: int) -> list[dict]:
    """Fetch funding history via raw HTTP for HIP-3 coins."""
    import requests
    from rift_core.config import get_proxy

    url = constants.MAINNET_API_URL
    url = url.rstrip("/") + "/info"
    proxies = {}
    proxy = get_proxy()
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    resp = requests.post(url, json={
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_time,
        "endTime": end_time,
    }, proxies=proxies, timeout=30)
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_candles(
    pair: str,
    interval: str,
    start_time: int | None = None,
    end_time: int | None = None,
) -> pl.DataFrame:
    """Fetch candles from Hyperliquid API and return as a Polars DataFrame.

    Args:
        pair: Trading pair (e.g. "BTC" or "BTC-PERP" — we strip the -PERP suffix)
        interval: Candle interval (1m, 5m, 15m, 1h, 4h, 1d, etc.)
        start_time: Start timestamp in ms. Defaults to 5000 candles ago.
        end_time: End timestamp in ms. Defaults to now.

    Returns:
        Polars DataFrame with columns: timestamp, open, high, low, close, volume, num_trades
    """
    if interval not in VALID_INTERVALS:
        raise ValueError(f"Invalid interval '{interval}'. Must be one of: {VALID_INTERVALS}")

    coin = normalize_coin(pair)
    use_raw = _is_hip3(coin)
    info = None if use_raw else get_info_client()

    if end_time is None:
        end_time = int(time.time() * 1000)
    if start_time is None:
        start_time = 0  # SDK will return most recent 5000 candles

    all_candles = []
    cursor_end = end_time

    while True:
        if use_raw:
            raw = _raw_candles_snapshot(coin, interval, start_time, cursor_end)
        else:
            raw = info.candles_snapshot(coin, interval, start_time, cursor_end)
        if not raw:
            break

        all_candles.extend(raw)

        # If we got fewer than 5000, we've reached the end
        if len(raw) < 5000:
            break

        # Move cursor back to fetch older candles
        oldest_ts = raw[0]["t"]
        if oldest_ts <= start_time:
            break
        cursor_end = oldest_ts - 1

    if not all_candles:
        return pl.DataFrame(
            schema={
                "timestamp": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
                "num_trades": pl.Int64,
            }
        )

    df = pl.DataFrame(
        {
            "timestamp": [c["t"] for c in all_candles],
            "open": [float(c["o"]) for c in all_candles],
            "high": [float(c["h"]) for c in all_candles],
            "low": [float(c["l"]) for c in all_candles],
            "close": [float(c["c"]) for c in all_candles],
            "volume": [float(c["v"]) for c in all_candles],
            "num_trades": [c["n"] for c in all_candles],
        }
    )

    # Deduplicate and sort
    df = df.unique(subset=["timestamp"]).sort("timestamp")
    return df


def save_candles(df: pl.DataFrame, pair: str, interval: str, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Save candle data to Parquet, partitioned by month."""
    coin = normalize_coin(pair)
    out_dir = data_dir / coin_to_path(coin) / interval
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "candles.parquet"

    # If file exists, merge with existing data
    if out_path.exists():
        existing = pl.read_parquet(out_path)
        if set(existing.columns) == set(df.columns):
            df = pl.concat([existing, df]).unique(subset=["timestamp"]).sort("timestamp")
        # else: schema changed (e.g. 7-col API → 18-col S3), overwrite with new

    df.write_parquet(out_path)

    # Write metadata
    meta = {
        "pair": coin,
        "interval": interval,
        "rows": len(df),
        "start": int(df["timestamp"].min()) if len(df) > 0 else 0,
        "end": int(df["timestamp"].max()) if len(df) > 0 else 0,
        "updated_at": int(time.time() * 1000),
    }
    meta_path = out_dir / "_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    return out_path


def fetch_funding_rates(
    pair: str,
    start_time: int | None = None,
    end_time: int | None = None,
) -> pl.DataFrame:
    """Fetch funding rate history from Hyperliquid API.

    Funding settles every hour on Hyperliquid. Returns a DataFrame with
    columns: timestamp, funding_rate, premium.
    """
    coin = normalize_coin(pair)
    use_raw = _is_hip3(coin)
    info = None if use_raw else get_info_client()

    if end_time is None:
        end_time = int(time.time() * 1000)
    if start_time is None:
        start_time = 0

    all_funding = []
    cursor_start = start_time

    while cursor_start < end_time:
        if use_raw:
            raw = _raw_funding_history(coin, cursor_start, end_time)
        else:
            raw = info.funding_history(coin, cursor_start, end_time)
        if not raw:
            break

        all_funding.extend(raw)

        # Move cursor forward past the last entry
        latest_ts = max(f["time"] for f in raw)
        if latest_ts <= cursor_start:
            break
        cursor_start = latest_ts + 1

        # If we got fewer than expected, we've reached the end
        if len(raw) < 500:
            break

    if not all_funding:
        return pl.DataFrame(schema={"timestamp": pl.Int64, "funding_rate": pl.Float64, "premium": pl.Float64})

    df = pl.DataFrame({
        "timestamp": [f["time"] for f in all_funding],
        "funding_rate": [float(f["fundingRate"]) for f in all_funding],
        "premium": [float(f["premium"]) for f in all_funding],
    })

    return df.unique(subset=["timestamp"]).sort("timestamp")


def save_funding_rates(df: pl.DataFrame, pair: str, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Save funding rate data to Parquet."""
    coin = normalize_coin(pair)
    out_dir = data_dir / coin_to_path(coin) / "funding"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "rates.parquet"

    if out_path.exists():
        existing = pl.read_parquet(out_path)
        df = pl.concat([existing, df]).unique(subset=["timestamp"]).sort("timestamp")

    df.write_parquet(out_path)
    return out_path


def load_funding_rates(pair: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load cached funding rate data."""
    coin = normalize_coin(pair)
    path = data_dir / coin_to_path(coin) / "funding" / "rates.parquet"
    if not path.exists():
        return None
    return pl.read_parquet(path)


def load_candles(pair: str, interval: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load cached candle data from Parquet."""
    coin = normalize_coin(pair)
    path = data_dir / coin_to_path(coin) / interval / "candles.parquet"
    if not path.exists():
        return None
    return pl.read_parquet(path)


# ─── MARKET CONTEXT (OI, Premium, Volume, etc.) ──────────────

def fetch_market_context(coin: str, info: Info | None = None) -> dict:
    """Fetch real-time market context for a coin from metaAndAssetCtxs.

    Returns dict with:
        open_interest: float   — total OI in contracts
        premium: float         — mark vs oracle premium (market bias)
        mark_price: float      — current mark price
        oracle_price: float    — oracle price
        day_volume: float      — 24h notional volume
        prev_day_price: float  — previous day close
        impact_bid: float      — impact bid price (liquidity depth)
        impact_ask: float      — impact ask price (liquidity depth)
        funding: float         — current funding rate
    """
    try:
        if info is None:
            info = get_info_client()
        data = info.meta_and_asset_ctxs()
        universe = data[0]["universe"]
        ctxs = data[1]

        for i, asset in enumerate(universe):
            if asset["name"] == coin and i < len(ctxs):
                ctx = ctxs[i]
                impact_bid = 0.0
                impact_ask = 0.0
                if ctx.get("impactPxs"):
                    impact_bid = float(ctx["impactPxs"][0])
                    impact_ask = float(ctx["impactPxs"][1])
                return {
                    "open_interest": float(ctx.get("openInterest", "0")),
                    "premium": float(ctx.get("premium", "0")),
                    "mark_price": float(ctx.get("markPx", "0")),
                    "oracle_price": float(ctx.get("oraclePx", "0")),
                    "day_volume": float(ctx.get("dayNtlVlm", "0")),
                    "prev_day_price": float(ctx.get("prevDayPx", "0")),
                    "impact_bid": impact_bid,
                    "impact_ask": impact_ask,
                    "funding": float(ctx.get("funding", "0")),
                }
        return {}
    except Exception:
        return {}


def fetch_market_breadth(info: Info | None = None, top_n: int = 20) -> dict:
    """Compute market breadth from RSI of the top N coins by volume.

    Fetches current prices for top coins, computes a simple RSI proxy
    from the price change, and returns crowd overbought/oversold percentages.

    For live/simulate use — called every 5 seconds with market context.
    Returns {overbought_pct, oversold_pct, avg_rsi}.
    """
    try:
        if info is None:
            info = get_info_client()
        data = info.meta_and_asset_ctxs()
        universe = data[0]["universe"]
        ctxs = data[1]

        # Get top N coins by volume with their price data
        coins = []
        for i, asset in enumerate(universe):
            if i < len(ctxs):
                vol = float(ctxs[i].get("dayNtlVlm", "0"))
                mark = float(ctxs[i].get("markPx", "0") or "0")
                prev = float(ctxs[i].get("prevDayPx", "0") or "0")
                if mark > 0 and prev > 0 and vol > 0:
                    coins.append({"name": asset["name"], "vol": vol, "mark": mark, "prev": prev})

        coins.sort(key=lambda x: x["vol"], reverse=True)
        top_coins = coins[:top_n]

        if len(top_coins) < 5:
            return {"overbought_pct": 0.0, "oversold_pct": 0.0, "avg_rsi": 50.0}

        # Compute RSI proxy from 24h price change
        # Positive change → higher RSI, negative → lower RSI
        # Map % change to approximate RSI: +10% ≈ RSI 80, -10% ≈ RSI 20
        rsis = []
        for c in top_coins:
            pct_change = (c["mark"] - c["prev"]) / c["prev"] * 100
            # Linear mapping: -10% → RSI 20, 0% → RSI 50, +10% → RSI 80
            approx_rsi = max(0, min(100, 50 + pct_change * 3))
            rsis.append(approx_rsi)

        ob_pct = sum(1 for r in rsis if r > 70) / len(rsis) * 100
        os_pct = sum(1 for r in rsis if r < 30) / len(rsis) * 100
        avg_rsi = sum(rsis) / len(rsis)

        return {
            "overbought_pct": ob_pct,
            "oversold_pct": os_pct,
            "avg_rsi": avg_rsi,
        }
    except Exception:
        return {"overbought_pct": 0.0, "oversold_pct": 0.0, "avg_rsi": 50.0}


def fetch_cross_exchange_funding(coin: str, info: Info | None = None) -> dict:
    """Fetch predicted funding rates across all exchanges.

    Returns dict with:
        hl: float       — Hyperliquid predicted rate
        binance: float  — Binance predicted rate
        bybit: float    — Bybit predicted rate
        hl_vs_cex: float — HL rate minus average CEX rate (divergence signal)
    """
    try:
        if info is None:
            info = get_info_client()
        data = info.post("/info", {"type": "predictedFundings"})

        hl_rate = 0.0
        bin_rate = 0.0
        bybit_rate = 0.0

        for entry in data:
            if entry[0] == coin:
                for venue in entry[1]:
                    name = venue[0]
                    if venue[1] is None:
                        continue
                    rate = float(venue[1]["fundingRate"])
                    if name == "HlPerp":
                        hl_rate = rate
                    elif name == "BinPerp":
                        bin_rate = rate
                    elif name == "BybitPerp":
                        bybit_rate = rate
                break

        # Divergence: how different is HL from CEX average
        cex_rates = [r for r in [bin_rate, bybit_rate] if r != 0]
        cex_avg = sum(cex_rates) / len(cex_rates) if cex_rates else 0.0
        divergence = hl_rate - cex_avg

        return {
            "hl": hl_rate,
            "binance": bin_rate,
            "bybit": bybit_rate,
            "hl_vs_cex": divergence,
        }
    except Exception:
        return {"hl": 0.0, "binance": 0.0, "bybit": 0.0, "hl_vs_cex": 0.0}


def fetch_oi_cap_assets(info: Info | None = None) -> list[str]:
    """Fetch assets currently at their open interest cap.

    When an asset hits its OI cap, no new positions can be opened on one side.
    This creates asymmetric pressure and often precedes sharp moves.
    """
    try:
        if info is None:
            info = get_info_client()
        data = info.post("/info", {"type": "perpsAtOpenInterestCap"})
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_borrow_rates(info: Info | None = None) -> dict[str, dict]:
    """Fetch borrow/lend rates for all tokens.

    High borrow utilization = demand for shorting.
    Sudden spikes in borrow rates can precede short squeezes.

    Returns {token: {borrow_rate, supply_rate, utilization, total_borrowed, total_supplied}}
    """
    try:
        if info is None:
            info = get_info_client()
        data = info.post("/info", {"type": "allBorrowLendReserveStates"})
        result = {}
        for entry in data:
            token = entry.get("coin", entry.get("token", ""))
            if not token:
                continue
            result[token] = {
                "borrow_rate": float(entry.get("borrowApy", "0")),
                "supply_rate": float(entry.get("supplyApy", "0")),
                "utilization": float(entry.get("utilization", "0")),
                "total_borrowed": float(entry.get("totalBorrowed", "0")),
                "total_supplied": float(entry.get("totalSupplied", "0")),
            }
        return result
    except Exception:
        return {}


# ─── MARKET SNAPSHOT STORAGE ──────────────────────────────────

SNAPSHOT_DIR = Path.home() / ".rift" / "data" / "_snapshots"


def save_market_snapshot(coin: str, context: dict, cross_funding: dict) -> None:
    """Save a point-in-time market snapshot for historical analysis.

    Called periodically during live/simulate to build historical OI,
    premium, and cross-exchange funding data for backtesting.
    """
    try:
        out_dir = SNAPSHOT_DIR / coin
        out_dir.mkdir(parents=True, exist_ok=True)

        snapshot = {
            "timestamp": int(time.time() * 1000),
            **context,
            "predicted_hl": cross_funding.get("hl", 0.0),
            "predicted_binance": cross_funding.get("binance", 0.0),
            "predicted_bybit": cross_funding.get("bybit", 0.0),
            "funding_divergence": cross_funding.get("hl_vs_cex", 0.0),
        }

        # Append to NDJSON file (one per day for easy management)
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d")
        file_path = out_dir / f"snapshots_{date_str}.ndjson"
        with open(file_path, "a") as f:
            f.write(json.dumps(snapshot) + "\n")
    except Exception:
        pass


def load_market_snapshots(coin: str) -> pl.DataFrame | None:
    """Load all saved market snapshots for a coin as a DataFrame."""
    snap_dir = SNAPSHOT_DIR / coin
    if not snap_dir.exists():
        return None

    rows = []
    for f in sorted(snap_dir.glob("snapshots_*.ndjson")):
        for line in f.read_text().strip().split("\n"):
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass

    if not rows:
        return None

    return pl.DataFrame(rows).unique(subset=["timestamp"]).sort("timestamp")


def fetch_predicted_funding(coin: str, info: Info | None = None) -> float:
    """Fetch predicted funding rate for the next settlement from Hyperliquid.

    Calls the predictedFundings endpoint (not in SDK — raw POST).
    Returns the HlPerp predicted rate for the given coin, or 0.0 on failure.

    The predicted rate continuously updates based on the current premium index
    and converges toward the actual rate as nextFundingTime approaches.
    """
    try:
        if info is None:
            info = get_info_client()
        data = info.post("/info", {"type": "predictedFundings"})
        for entry in data:
            if entry[0] == coin:
                for venue in entry[1]:
                    if venue[0] == "HlPerp" and venue[1] is not None:
                        return float(venue[1]["fundingRate"])
        return 0.0
    except Exception:
        return 0.0


def list_cached_data(data_dir: Path = DEFAULT_DATA_DIR) -> list[dict]:
    """List all cached data files."""
    results = []
    if not data_dir.exists():
        return results

    for coin_dir in sorted(data_dir.iterdir()):
        if not coin_dir.is_dir() or coin_dir.name.startswith(".") or coin_dir.name.startswith("_"):
            continue
        coin_name = path_to_coin(coin_dir.name)
        for interval_dir in sorted(coin_dir.iterdir()):
            if not interval_dir.is_dir():
                continue
            meta_path = interval_dir / "_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                meta["pair"] = coin_name  # ensure coin name is correct (not path-sanitized)
                results.append(meta)
    return results


def load_orderbook_snapshots(coin: str, start_time: int, end_time: int, data_dir: Path = DEFAULT_DATA_DIR) -> list[dict]:
    """Load orderbook snapshots from NDJSON files for a given coin + time range.

    Returns list of dicts with keys: timestamp, bids, asks.
    Each bid/ask is a list of [price, size] pairs (5 levels).
    """
    ob_dir = data_dir / "_orderbook"
    if not ob_dir.exists():
        return []

    coin_upper = normalize_coin(coin)
    results = []

    for f in sorted(ob_dir.glob("orderbook_*.ndjson")):
        try:
            with open(f) as fh:
                for line in fh:
                    snap = json.loads(line.strip())
                    if snap.get("coin") != coin_upper and snap.get("coin") != coin_upper.replace("xyz:", ""):
                        continue
                    ts = snap.get("timestamp", 0)
                    if start_time <= ts <= end_time:
                        results.append(snap)
        except Exception:
            continue

    return results
