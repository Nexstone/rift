"""Field catalog — declares every queryable field, its source, and how
to acquire it.

This is the single source of truth that Data.load() consults to:
  1. Validate that the user's requested fields exist
  2. Check whether the data for those fields is cached
  3. Produce actionable "fetch with X" messages when it isn't

When you add a new field to the substrate (e.g., new HL endpoint surfaces
a new derivative metric), add it here. Everything else picks it up.

Source types:
  candles    — OHLCV + volume from candle parquets (rift fetch + rift sync)
  funding    — funding rate history (rift fetch + rift sync)
  fills      — per-trade fills from S3 archive (rift sync only, AWS-paid)
  l2         — L2 orderbook snapshots from S3 archive (rift sync only)
  ctx        — asset context: OI, premium, etc. (rift fetch)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SourceType = Literal["candles", "funding", "fills", "l2", "ctx"]
IngestionMethod = Literal["fetch", "sync", "subscribe"]


@dataclass(frozen=True)
class FieldSpec:
    """Metadata for a queryable field."""
    name: str
    source: SourceType
    ingestion: IngestionMethod
    description: str
    units: str | None = None
    derived: bool = False  # True if computed from base fields (not stored)


# ─── The catalog ──────────────────────────────────────────────────────

# Order matters only for display — group by source for readability.
FIELDS: dict[str, FieldSpec] = {
    # OHLCV — from candle parquets
    "open":       FieldSpec("open",       "candles", "fetch", "Opening price of the bar", "USD"),
    "high":       FieldSpec("high",       "candles", "fetch", "Highest price in the bar", "USD"),
    "low":        FieldSpec("low",        "candles", "fetch", "Lowest price in the bar", "USD"),
    "close":      FieldSpec("close",      "candles", "fetch", "Closing price of the bar", "USD"),
    "volume":     FieldSpec("volume",     "candles", "fetch", "Volume traded in the bar (base units)"),
    "trades":     FieldSpec("trades",     "candles", "fetch", "Trade count in the bar"),

    # Funding
    "funding_rate": FieldSpec(
        "funding_rate", "funding", "fetch",
        "Hyperliquid hourly funding rate (positive = longs pay shorts)",
    ),

    # Asset context (from meta_and_asset_ctxs)
    "open_interest": FieldSpec("open_interest", "ctx", "fetch", "Total OI in contracts"),
    "oi_delta":      FieldSpec("oi_delta",      "ctx", "fetch", "OI change vs prior period"),
    "oi_zscore":     FieldSpec("oi_zscore",     "ctx", "fetch", "OI 30d rolling z-score", derived=True),
    "premium":       FieldSpec("premium",       "ctx", "fetch", "Mark price vs oracle (directional bias signal)"),
    "oracle_price":  FieldSpec("oracle_price",  "ctx", "fetch", "HL oracle price (spot reference)"),
    "day_volume":    FieldSpec("day_volume",    "ctx", "fetch", "Trailing 24h notional volume", "USD"),

    # Fills (ground-truth, from S3 archive)
    "buy_volume":     FieldSpec("buy_volume",     "fills", "sync", "Aggressor-buy volume per bar"),
    "sell_volume":    FieldSpec("sell_volume",    "fills", "sync", "Aggressor-sell volume per bar"),
    "taker_ratio":    FieldSpec("taker_ratio",    "fills", "sync", "Fraction of fills that crossed the spread (aggressor)"),
    "imbalance":      FieldSpec("imbalance",      "fills", "sync", "(buy_volume - sell_volume) / total per bar", derived=True),
    "opens_long":     FieldSpec("opens_long",     "fills", "sync", "Volume of new long positions opened"),
    "closes_long":    FieldSpec("closes_long",    "fills", "sync", "Volume of long positions closed"),
    "opens_short":    FieldSpec("opens_short",    "fills", "sync", "Volume of new short positions opened"),
    "closes_short":   FieldSpec("closes_short",   "fills", "sync", "Volume of short positions closed"),
    "net_flow":       FieldSpec("net_flow",       "fills", "sync", "(opens - closes) per bar", derived=True),
    "candle_pnl":     FieldSpec("candle_pnl",     "fills", "sync", "Total realized PnL by all traders per bar", "USD"),
    "candle_fees":    FieldSpec("candle_fees",    "fills", "sync", "Total fees paid per bar", "USD"),

    # L2 microstructure (from S3 snapshots, 5-min resolution)
    "bid_depth_5":  FieldSpec("bid_depth_5",  "l2", "sync", "Total bid volume across top 5 levels"),
    "ask_depth_5":  FieldSpec("ask_depth_5",  "l2", "sync", "Total ask volume across top 5 levels"),
    "spread_bps":   FieldSpec("spread_bps",   "l2", "sync", "Bid-ask spread in basis points", "bps"),
    "depth_ratio":  FieldSpec("depth_ratio",  "l2", "sync", "bid_depth / ask_depth (>1 = bid-heavy)", derived=True),
    "bid_ask_imbalance": FieldSpec(
        "bid_ask_imbalance", "l2", "sync",
        "(bid_vol - ask_vol) / total per snapshot, range [-1, +1]",
        derived=True,
    ),
}


# ─── Convenience helpers ──────────────────────────────────────────────


def field_requires(name: str) -> tuple[SourceType, IngestionMethod]:
    """Return (source, ingestion) for a field, or raise KeyError."""
    spec = FIELDS.get(name)
    if spec is None:
        raise KeyError(
            f"Unknown field '{name}'. Available: {sorted(FIELDS.keys())}"
        )
    return spec.source, spec.ingestion


def fields_by_source(source: SourceType) -> list[FieldSpec]:
    """All fields backed by a given source."""
    return [f for f in FIELDS.values() if f.source == source]


def fields_by_ingestion(method: IngestionMethod) -> list[FieldSpec]:
    """All fields that require a given ingestion method."""
    return [f for f in FIELDS.values() if f.ingestion == method]


def list_available_fields() -> list[str]:
    return sorted(FIELDS.keys())


# Map ingestion method → CLI command hint
INGESTION_HINTS = {
    "fetch":     "rift fetch <COIN> --tf <TF>  (free, HL info endpoint)",
    "sync":      "rift sync --coins <COIN> [--include-fills | --include-l2]  (HL S3 archive, AWS-paid)",
    "subscribe": "rift subscribe <COIN> --channels <ch>  (free, real-time websocket)",
}


def hint_for_field(name: str) -> str:
    """Return an actionable acquisition hint for a field."""
    spec = FIELDS.get(name)
    if spec is None:
        return f"Unknown field '{name}'"
    return INGESTION_HINTS.get(spec.ingestion, "Unknown ingestion method")
