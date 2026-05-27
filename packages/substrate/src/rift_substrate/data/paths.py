"""Canonical cache paths for HL data on disk.

Single source of truth for where each data source lives under ~/.rift/data/.
Used by inventory (to scan) and access (to load).

Layout:
  ~/.rift/data/
  ├── <COIN>/                              # e.g. BTC, ETH, SOL
  │   ├── <TF>/                            # 1m, 5m, 15m, 30m, 1h, 4h, 1d
  │   │   └── candles.parquet              # OHLCV + funding + asset ctx fields
  │   ├── funding/
  │   │   └── rates.parquet                # full funding history
  │   ├── fills/
  │   │   └── YYYYMMDD.parquet             # one file per UTC day
  │   ├── l2/                              # optional, from sync
  │   │   └── YYYYMMDD.parquet             # one file per UTC day
  │   └── coinalyze/                       # third-party context (optional)
  │       └── *.parquet
  └── _snapshots/, _orderbook/, etc.       # ancillary; not exposed via Data.load
"""

from __future__ import annotations

from pathlib import Path


DATA_DIR = Path.home() / ".rift" / "data"


def coin_dir(coin: str) -> Path:
    """Top-level dir for a coin's cached data."""
    return DATA_DIR / coin.upper()


def candles_path(coin: str, tf: str) -> Path:
    """Path to candles parquet for coin+timeframe.

    e.g. candles_path("BTC", "1h") = ~/.rift/data/BTC/1h/candles.parquet
    """
    return coin_dir(coin) / tf / "candles.parquet"


def funding_path(coin: str) -> Path:
    return coin_dir(coin) / "funding" / "rates.parquet"


def fills_dir(coin: str) -> Path:
    return coin_dir(coin) / "fills"


def l2_dir(coin: str) -> Path:
    return coin_dir(coin) / "l2"


def fill_files(coin: str) -> list[Path]:
    """Sorted list of per-day fill parquets for a coin."""
    d = fills_dir(coin)
    if not d.is_dir():
        return []
    return sorted(d.glob("????????.parquet"))


def l2_files(coin: str) -> list[Path]:
    """Sorted list of per-day L2 parquets for a coin."""
    d = l2_dir(coin)
    if not d.is_dir():
        return []
    return sorted(d.glob("????????.parquet"))


def cached_timeframes(coin: str) -> list[str]:
    """Timeframes for which candles are cached for a coin.

    Discovers from disk — anything matching <coin_dir>/<TF>/candles.parquet.
    """
    cd = coin_dir(coin)
    if not cd.is_dir():
        return []
    tfs = []
    for entry in cd.iterdir():
        if entry.is_dir() and (entry / "candles.parquet").exists():
            tfs.append(entry.name)
    return sorted(tfs, key=_tf_sort_key)


def cached_coins() -> list[str]:
    """All coins with any cached data. Skips underscored ancillary dirs."""
    if not DATA_DIR.is_dir():
        return []
    return sorted(
        d.name for d in DATA_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


def _tf_sort_key(tf: str) -> tuple[int, str]:
    """Sort timeframes by their natural size: 1m < 5m < 15m < 1h < 1d."""
    units = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    for suffix, mult in units.items():
        if tf.endswith(suffix):
            try:
                return (int(tf[:-len(suffix)]) * mult, tf)
            except ValueError:
                pass
    return (0, tf)
