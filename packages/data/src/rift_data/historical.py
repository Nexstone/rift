"""Historical data loader for RIFT.

Data comes from Hyperliquid S3 via `rift sync`. Local cache at ~/.rift/data/.
No bundled data — users sync on first run.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl


DEFAULT_DATA_DIR = Path.home() / ".rift" / "data"


def load_candles_smart(coin: str, interval: str = "1h", source: str = "auto") -> pl.DataFrame | None:
    """Load candle data from local cache (synced from S3).

    Args:
        coin: Coin name (e.g., "BTC", "xyz:SP500")
        interval: Candle interval ("5m", "1h", "4h", etc.)
        source: "auto" or "hyperliquid" (legacy, both read local cache)

    Returns:
        DataFrame or None if no data synced for this coin/interval
    """
    from rift_data.data import load_candles
    from rift_core.schema import normalize_coin

    coin = normalize_coin(coin)
    return load_candles(coin, interval)


def load_funding_smart(coin: str, source: str = "auto") -> pl.DataFrame | None:
    """Load funding data from local cache (synced from S3).

    Args:
        coin: Coin name
        source: "auto" (legacy param, reads local cache)

    Returns:
        DataFrame or None
    """
    from rift_data.data import load_funding_rates
    from rift_core.schema import normalize_coin

    coin = normalize_coin(coin)
    return load_funding_rates(coin)


def load_fills(coin: str) -> pl.DataFrame | None:
    """Load raw tick-level fills from local cache (synced from S3).

    Prefers the new partitioned daily layout
    (~/.rift/data/{coin}/fills/{YYYYMMDD}.parquet) and falls back to the
    legacy single-file layout (~/.rift/data/{coin}/fills/fills.parquet).
    If both exist, daily files take precedence (legacy is treated as stale).

    Returns:
        DataFrame with columns: timestamp, price, size, side, dir, is_open,
        is_long, crossed, closed_pnl, fee, start_position
    """
    from rift_core.schema import normalize_coin, coin_to_path

    coin = normalize_coin(coin)
    fills_dir = DEFAULT_DATA_DIR / coin_to_path(coin) / "fills"
    if not fills_dir.exists():
        return None

    daily_files = sorted(
        p for p in fills_dir.glob("*.parquet")
        if p.stem.isdigit() and len(p.stem) == 8
    )
    if daily_files:
        return pl.read_parquet([str(p) for p in daily_files])

    legacy = fills_dir / "fills.parquet"
    if legacy.exists():
        return pl.read_parquet(legacy)
    return None


def scan_fills(coin: str):
    """Lazy scan of partitioned daily fill files for memory-bounded queries.

    Use when you only need a date range or subset of columns — supports
    polars predicate/projection pushdown so the full multi-GB dataset is
    never materialized.

    Returns a polars LazyFrame, or None if no fills are cached.
    """
    from rift_core.schema import normalize_coin, coin_to_path

    coin = normalize_coin(coin)
    fills_dir = DEFAULT_DATA_DIR / coin_to_path(coin) / "fills"
    if not fills_dir.exists():
        return None

    daily_files = sorted(
        str(p) for p in fills_dir.glob("*.parquet")
        if p.stem.isdigit() and len(p.stem) == 8
    )
    if daily_files:
        return pl.scan_parquet(daily_files)

    legacy = fills_dir / "fills.parquet"
    if legacy.exists():
        return pl.scan_parquet(str(legacy))
    return None
