"""Fills → OHLCV candles with ground-truth order flow.

S3 records BOTH sides of every trade (taker + maker), so we filter to taker
fills (crossed=True) before aggregation to avoid double-counting volume.
The taker's direction tells us actual market pressure.

Output candle schema (18 columns when full flow data is present):
  timestamp, open, high, low, close, volume, num_trades,
  buy_volume, sell_volume, volume_delta, taker_ratio,
  opens_long, closes_long, opens_short, closes_short, net_flow,
  total_pnl, total_fees
"""

from __future__ import annotations

from typing import Callable

import polars as pl


def fills_to_candles(fills: pl.DataFrame, interval: str) -> pl.DataFrame:
    """Aggregate raw fills into OHLCV candles with ground-truth order flow.

    Filters to taker fills (crossed=True) to avoid the double-count from S3
    recording both sides of every trade.
    """
    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
        "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
        "2d": 172_800_000, "3d": 259_200_000, "1w": 604_800_000,
    }
    ms = interval_ms.get(interval)
    if ms is None:
        raise ValueError(f"Unknown interval: {interval}")

    if len(fills) == 0:
        return pl.DataFrame(schema={
            "timestamp": pl.Int64, "open": pl.Float64, "high": pl.Float64,
            "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
            "num_trades": pl.Int64,
        })

    has_crossed = "crossed" in fills.columns
    has_flow = "is_open" in fills.columns and "is_long" in fills.columns

    if has_crossed:
        taker_fills = fills.filter(pl.col("crossed"))
    else:
        taker_fills = fills

    agg_exprs = [
        pl.col("price").first().alias("open"),
        pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"),
        pl.col("price").last().alias("close"),
        pl.col("size").sum().alias("volume"),
        pl.len().cast(pl.Int64).alias("num_trades"),
    ]

    if has_flow:
        buy_filter = (pl.col("is_open") & pl.col("is_long")) | (~pl.col("is_open") & ~pl.col("is_long"))
        sell_filter = (pl.col("is_open") & ~pl.col("is_long")) | (~pl.col("is_open") & pl.col("is_long"))
        agg_exprs.extend([
            pl.col("size").filter(buy_filter).sum().alias("buy_volume"),
            pl.col("size").filter(sell_filter).sum().alias("sell_volume"),
        ])
        agg_exprs.append(pl.lit(1.0).alias("taker_ratio"))
        agg_exprs.extend([
            pl.col("size").filter(pl.col("is_open") & pl.col("is_long")).sum().alias("opens_long"),
            pl.col("size").filter(~pl.col("is_open") & pl.col("is_long")).sum().alias("closes_long"),
            pl.col("size").filter(pl.col("is_open") & ~pl.col("is_long")).sum().alias("opens_short"),
            pl.col("size").filter(~pl.col("is_open") & ~pl.col("is_long")).sum().alias("closes_short"),
        ])

    if "closed_pnl" in taker_fills.columns:
        agg_exprs.append(pl.col("closed_pnl").sum().alias("total_pnl"))
    if "fee" in taker_fills.columns:
        agg_exprs.append(pl.col("fee").sum().alias("total_fees"))

    candles = (
        taker_fills
        .with_columns((pl.col("timestamp") // ms * ms).alias("candle_ts"))
        .group_by("candle_ts")
        .agg(agg_exprs)
        .rename({"candle_ts": "timestamp"})
        .sort("timestamp")
    )

    if has_flow and "buy_volume" in candles.columns:
        candles = candles.with_columns(
            (pl.col("buy_volume") - pl.col("sell_volume")).alias("volume_delta"),
        )
    if has_flow and "opens_long" in candles.columns:
        candles = candles.with_columns(
            (pl.col("opens_long") - pl.col("closes_long")
             - pl.col("opens_short") + pl.col("closes_short")).alias("net_flow"),
        )
    return candles


def flush_candle_buffers(
    buffers: dict[tuple[str, str], list[pl.DataFrame]],
    on_progress: Callable | None = None,
) -> None:
    """Concat per-(coin, tf) per-day candle slices and persist via save_candles.

    Buffers are cleared after flush.
    """
    from rift_data.data import save_candles

    for (norm_coin, tf), parts in buffers.items():
        if not parts:
            continue
        merged = pl.concat(parts).sort("timestamp")
        if len(merged) == 0:
            continue
        save_candles(merged, norm_coin, tf)
        if on_progress:
            on_progress(f"  flushed {norm_coin} {tf}: {len(merged):,} new rows")
    buffers.clear()
