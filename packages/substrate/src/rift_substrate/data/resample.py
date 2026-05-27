"""Bar aggregation — time, volume, dollar bars.

Three bar constructions:
  time:   new bar every N seconds/minutes/hours/days (standard)
  volume: new bar every N units of base-currency volume (equal-information bars)
  dollar: new bar every N USD notional (price-invariant equal-information bars)

Volume and dollar bars are info-content normalized: each bar represents
roughly equal trading activity, so returns are closer to IID-normal,
which makes statistical tests + ML models work better.

Input shape:
  Both lazy and eager polars frames with columns:
    timestamp (ms, int64), open, high, low, close (float), volume (float)
  Additional fields pass through but are not aggregated by this module.

Output shape:
  Aggregated DataFrame with the same OHLCV schema. Timestamp = bar END time.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import polars as pl


# ─── Time bars ────────────────────────────────────────────────────────


def parse_time_freq(freq: str) -> str:
    """Convert RIFT freq string ("1h", "15m", "1d") to polars duration string.

    Polars uses suffixes: s, m (minutes), h, d, w. Our convention matches.
    """
    # Validate
    valid_suffixes = ("s", "m", "h", "d", "w")
    if not freq[-1] in valid_suffixes:
        raise ValueError(
            f"Time freq must end in one of {valid_suffixes}; got '{freq}'"
        )
    try:
        int(freq[:-1])
    except ValueError as e:
        raise ValueError(f"Time freq prefix must be int; got '{freq}'") from e
    return freq


def to_time_bars(
    df: pl.DataFrame | pl.LazyFrame,
    freq: str,
    timestamp_col: str = "timestamp",
) -> pl.DataFrame:
    """Resample a candle/tick frame to time bars at the given freq.

    Assumes input is already at finer resolution than target (or equal).
    For raw fills, use freq like "1m"; the function aggregates per bar.
    """
    polars_freq = parse_time_freq(freq)
    is_lazy = isinstance(df, pl.LazyFrame)
    if is_lazy:
        df = df.collect()

    # Ensure timestamp is datetime for group_by_dynamic
    if df[timestamp_col].dtype != pl.Datetime:
        df = df.with_columns(
            pl.from_epoch(timestamp_col, time_unit="ms").alias("_ts_dt")
        )
        ts_col = "_ts_dt"
    else:
        ts_col = timestamp_col

    df = df.sort(ts_col)

    agg_exprs = []
    if "open" in df.columns:
        agg_exprs.append(pl.col("open").first().alias("open"))
    if "high" in df.columns:
        agg_exprs.append(pl.col("high").max().alias("high"))
    if "low" in df.columns:
        agg_exprs.append(pl.col("low").min().alias("low"))
    if "close" in df.columns:
        agg_exprs.append(pl.col("close").last().alias("close"))
    if "volume" in df.columns:
        agg_exprs.append(pl.col("volume").sum().alias("volume"))
    if "trades" in df.columns:
        agg_exprs.append(pl.col("trades").sum().alias("trades"))

    out = df.group_by_dynamic(ts_col, every=polars_freq, closed="left").agg(agg_exprs)

    # Convert datetime back to ms timestamp for consistency
    out = out.with_columns(
        out[ts_col].dt.timestamp(time_unit="ms").alias("timestamp")
    )
    if ts_col == "_ts_dt":
        out = out.drop("_ts_dt")
    elif timestamp_col != "timestamp":
        out = out.drop(ts_col)

    # Move timestamp to first column
    cols = ["timestamp"] + [c for c in out.columns if c != "timestamp"]
    return out.select(cols)


# ─── Volume / dollar bars ─────────────────────────────────────────────


def _accumulator_bars(
    df: pl.DataFrame,
    threshold: float,
    accumulator_col: str,
    timestamp_col: str = "timestamp",
) -> pl.DataFrame:
    """Generic accumulator-bar builder.

    Walks the input row-by-row, accumulating `accumulator_col`. When the
    accumulator crosses `threshold`, close the bar and start a new one.

    For volume bars: accumulator_col = "volume"
    For dollar bars: caller should add a "_dollar" column = close * volume
                     before calling, then pass accumulator_col="_dollar"
    """
    if accumulator_col not in df.columns:
        raise ValueError(f"Column '{accumulator_col}' not found in input")

    df = df.sort(timestamp_col)

    timestamps = df[timestamp_col].to_numpy()
    accumulator = df[accumulator_col].to_numpy()

    # Optional fields
    opens = df["open"].to_numpy() if "open" in df.columns else None
    highs = df["high"].to_numpy() if "high" in df.columns else None
    lows = df["low"].to_numpy() if "low" in df.columns else None
    closes = df["close"].to_numpy() if "close" in df.columns else None
    volumes = df["volume"].to_numpy() if "volume" in df.columns else None

    out_ts: list[int] = []
    out_open: list[float] = []
    out_high: list[float] = []
    out_low: list[float] = []
    out_close: list[float] = []
    out_volume: list[float] = []
    out_accumulator: list[float] = []

    n = len(df)
    if n == 0:
        return df.head(0)

    i = 0
    while i < n:
        # Start a new bar
        bar_start_idx = i
        bar_acc = 0.0
        bar_open = float(opens[i]) if opens is not None else float(closes[i] if closes is not None else 0)
        bar_high = float(highs[i]) if highs is not None else bar_open
        bar_low = float(lows[i]) if lows is not None else bar_open
        bar_close = bar_open
        bar_volume = 0.0

        while i < n and bar_acc < threshold:
            bar_acc += float(accumulator[i])
            if highs is not None and highs[i] > bar_high:
                bar_high = float(highs[i])
            if lows is not None and lows[i] < bar_low:
                bar_low = float(lows[i])
            if closes is not None:
                bar_close = float(closes[i])
            if volumes is not None:
                bar_volume += float(volumes[i])
            i += 1

        out_ts.append(int(timestamps[i - 1]))   # bar END time
        out_open.append(bar_open)
        out_high.append(bar_high)
        out_low.append(bar_low)
        out_close.append(bar_close)
        out_volume.append(bar_volume)
        out_accumulator.append(bar_acc)

    return pl.DataFrame({
        "timestamp": out_ts,
        "open": out_open,
        "high": out_high,
        "low": out_low,
        "close": out_close,
        "volume": out_volume,
    })


def to_volume_bars(
    df: pl.DataFrame | pl.LazyFrame,
    threshold_units: float,
    timestamp_col: str = "timestamp",
) -> pl.DataFrame:
    """Aggregate to volume bars — new bar every `threshold_units` of base volume.

    Example:
      to_volume_bars(btc_1m_candles, threshold_units=100)
      → new bar each time 100 BTC have been traded.

    Input source:
      - Aggregated candles (volume = sum of trade sizes in that bar)
      - Raw fills (volume = trade size)
      Both work; raw fills give the finest resolution.
    """
    if threshold_units <= 0:
        raise ValueError(f"threshold_units must be > 0; got {threshold_units}")
    if isinstance(df, pl.LazyFrame):
        df = df.collect()
    if "volume" not in df.columns:
        raise ValueError("volume column required for volume bars")
    return _accumulator_bars(df, threshold_units, "volume", timestamp_col)


def to_dollar_bars(
    df: pl.DataFrame | pl.LazyFrame,
    threshold_usd: float,
    timestamp_col: str = "timestamp",
) -> pl.DataFrame:
    """Aggregate to dollar bars — new bar every `threshold_usd` of notional.

    Example:
      to_dollar_bars(btc_1m_candles, threshold_usd=1_000_000)
      → new bar each time $1M notional has been traded.

    Dollar bars are price-invariant: a "$1M bar" means the same thing
    whether BTC is $20k or $100k. Best for long-history backtests and
    cross-asset comparisons.

    Implementation note:
      We approximate per-row dollar volume as close * volume. For aggregated
      candle inputs (1m+) this is a slight underestimate vs walking each
      tick, but close enough for practical aggregation.
    """
    if threshold_usd <= 0:
        raise ValueError(f"threshold_usd must be > 0; got {threshold_usd}")
    if isinstance(df, pl.LazyFrame):
        df = df.collect()
    if "close" not in df.columns or "volume" not in df.columns:
        raise ValueError("close + volume columns required for dollar bars")

    df_with_dollar = df.with_columns(
        (pl.col("close") * pl.col("volume")).alias("_dollar")
    )
    out = _accumulator_bars(df_with_dollar, threshold_usd, "_dollar", timestamp_col)
    return out
