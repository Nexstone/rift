"""Shared schema: coin name normalization and the canonical fill DataFrame schema.

Used by every rift_* package that touches coin identifiers or fill data.
"""

from __future__ import annotations

import polars as pl


# Known HIP-3 TradFi tickers — allows users to type "SP500" instead of "xyz:SP500"
_KNOWN_TRADFI = {
    "SP500", "XYZ100", "JP225", "KR200", "NIFTY",
    "TSLA", "NVDA", "AAPL", "GOOGL", "AMD", "META", "AMZN", "MSFT", "INTC",
    "COIN", "HOOD", "PLTR", "MSTR", "GME", "NFLX", "ARM", "TSM",
    "BABA", "ORCL", "HIMS", "CRWV", "RKLB", "MRVL", "LLY", "COST",
    "RIVN", "ZM", "EBAY", "DKNG", "BX", "BIRD", "CRCL", "LITE", "MU", "SNDK",
    "SKHX", "SMSN", "HYUNDAI", "SOFTBANK", "KIOXIA",
    "EWY", "EWJ", "EWZ", "XLE", "URNM",
    "CL", "GOLD", "SILVER", "COPPER", "NATGAS", "BRENTOIL", "PLATINUM", "PALLADIUM",
    "URANIUM", "ALUMINIUM", "CORN", "WHEAT", "TTF", "DRAM",
    "EUR", "JPY", "KRW", "DXY",
    "VIX", "VOL", "H100",
}


VALID_INTERVALS = [
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "8h", "12h",
    "1d", "3d", "1w", "1M",
]


# Canonical schema for raw fills written to ~/.rift/data/{coin}/fills/{YYYYMMDD}.parquet
FILL_SCHEMA = {
    "timestamp": pl.Int64,
    "price": pl.Float64,
    "size": pl.Float64,
    "side": pl.Utf8,
    "dir": pl.Utf8,
    "is_open": pl.Boolean,
    "is_long": pl.Boolean,
    "crossed": pl.Boolean,
    "closed_pnl": pl.Float64,
    "fee": pl.Float64,
    "start_position": pl.Float64,
}


def normalize_coin(pair: str) -> str:
    """Normalize a pair/coin input to its API coin name.

    Strips -PERP suffix, preserves xyz: prefix for HIP-3 TradFi perps.
    Examples: "BTC-PERP" → "BTC", "xyz:SP500" → "xyz:SP500", "sp500" → "xyz:SP500"
    """
    coin = pair.replace("-PERP", "").replace("-perp", "").strip()
    # Common TradFi tickers — auto-add xyz: prefix if missing
    if coin.upper() in _KNOWN_TRADFI:
        return f"xyz:{coin.upper()}"
    # Preserve existing xyz: prefix
    if coin.startswith("xyz:"):
        return f"xyz:{coin[4:].upper()}"
    return coin.upper()


def coin_to_path(coin: str) -> str:
    """Convert coin name to filesystem-safe directory name.

    xyz:SP500 → xyz-SP500, BTC → BTC, HYPE/USDC → spot-HYPE-USDC
    """
    if "/" in coin:
        return "spot-" + coin.replace("/", "-")
    return coin.replace(":", "-")


def path_to_coin(dirname: str) -> str:
    """Convert filesystem directory name back to coin name.

    xyz-SP500 → xyz:SP500, BTC → BTC, spot-HYPE-USDC → HYPE/USDC
    """
    if dirname.startswith("spot-"):
        parts = dirname[5:].split("-")
        return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else dirname
    if dirname.startswith("xyz-"):
        return f"xyz:{dirname[4:]}"
    return dirname


def detect_market(coin: str) -> str:
    """Detect if a coin reference is spot or perps.

    'HYPE/USDC' → 'spot', 'HYPE' → 'perp', 'xyz:SP500' → 'perp'
    """
    if "/" in coin:
        return "spot"
    return "perp"


def normalize_spot(coin: str) -> str:
    """Normalize a coin name for spot trading.

    'HYPE' → 'HYPE/USDC', 'hype' → 'HYPE/USDC', 'HYPE/USDC' → 'HYPE/USDC'
    """
    coin = coin.strip().upper()
    if "/" not in coin:
        return f"{coin}/USDC"
    return coin
