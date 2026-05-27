"""Coinalyze cache readers (read-only).

Historically this module also fetched data from the Coinalyze API. That
ingestion path is operated by the upstream maintainer outside the repo,
not by OSS users — so the API-fetch / `COINALYZE_API_KEY` code was
removed. What remains is the read side: load pre-existing parquet files
from `~/.rift/data/<COIN>/historical/`.

If the files don't exist (the common OSS case), every loader returns
`None` and callers gracefully degrade — there's no API key to configure
and no fetch step to fail.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl


DEFAULT_DATA_DIR = Path.home() / ".rift" / "data"


def load_hl_historical_candles(coin: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load historical daily candles."""
    path = data_dir / coin.upper() / "historical" / "candles_daily.parquet"
    if path.exists():
        return pl.read_parquet(path)
    return None


def load_hl_historical_oi(coin: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load historical OI data."""
    path = data_dir / coin.upper() / "historical" / "oi_daily.parquet"
    if path.exists():
        return pl.read_parquet(path)
    return None


def load_hl_historical_funding(coin: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load historical funding rates."""
    path = data_dir / coin.upper() / "historical" / "funding.parquet"
    if path.exists():
        return pl.read_parquet(path)
    return None


def load_hl_historical_liquidations(coin: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load historical liquidation data."""
    path = data_dir / coin.upper() / "historical" / "liquidations.parquet"
    if path.exists():
        return pl.read_parquet(path)
    return None


def load_hl_historical_ls_ratio(coin: str, data_dir: Path = DEFAULT_DATA_DIR) -> pl.DataFrame | None:
    """Load historical long/short ratio."""
    path = data_dir / coin.upper() / "historical" / "ls_ratio.parquet"
    if path.exists():
        return pl.read_parquet(path)
    return None
