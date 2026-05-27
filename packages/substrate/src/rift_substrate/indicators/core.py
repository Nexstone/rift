"""Polars-native indicator wrappers around pandas-ta-classic.

Each function accepts polars Series, pandas Series, numpy arrays, or
python lists and returns a polars Series. Multi-output indicators
(bbands, macd, stoch, supertrend) return a polars DataFrame so callers
can `df.hstack(result)` cleanly.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pandas_ta_classic as ta
import polars as pl


# ─── Coercion ─────────────────────────────────────────────────────────


def _to_pd_series(x: Any, name: str = "x") -> pd.Series:
    """Coerce polars Series / numpy / list / pandas Series to a pandas Series."""
    if isinstance(x, pl.Series):
        return x.to_pandas()
    if isinstance(x, pd.Series):
        return x
    return pd.Series(np.asarray(x, dtype=float), name=name)


def _to_pl_series(s: pd.Series | None, name: str, input_length: int) -> pl.Series:
    """Convert pandas Series → polars Series with the given name.

    pandas-ta returns None when the input is too short to compute the
    indicator. We pad with nulls to match the input length so the output
    is always row-aligned with the input — downstream code can rely on
    `len(rsi(close)) == len(close)`. NaN values from pandas (warm-up
    windows) are mapped to polars null so `.drop_nulls()` works.
    """
    if s is None:
        return pl.Series(name=name, values=[None] * input_length, dtype=pl.Float64)
    return pl.Series(name=name, values=s.values).fill_nan(None)


def _smart_rename(df: pd.DataFrame, prefix_map: dict[str, str]) -> pd.DataFrame:
    """Rename pandas-ta-classic output columns by matching prefix.

    pandas-ta-classic names its columns with the parameters baked in
    (e.g. `BBL_20_2.0`, `MACDh_12_26_9`). Hardcoding those exact strings
    makes us fragile to any change in their suffix format. Instead, we
    match by an unambiguous prefix (e.g. `BBL_`, `MACDh_`) — every
    pandas-ta-classic output we care about has a distinct prefix, and
    the trailing underscore disambiguates `MACD_` from `MACDh_`/`MACDs_`.

    Any column that doesn't match a prefix in `prefix_map` is left alone.
    """
    actual: dict[str, str] = {}
    for col in df.columns:
        # Longest matching prefix wins (avoids `MACD_` matching `MACDh_…`).
        best: tuple[str, str] | None = None
        for prefix, target in prefix_map.items():
            if col.startswith(prefix):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, target)
        if best is not None:
            actual[col] = best[1]
    return df.rename(columns=actual)


def _to_pl_frame(
    df: pd.DataFrame | None,
    prefix_map: dict[str, str] | None,
    input_length: int,
) -> pl.DataFrame:
    """Convert pandas DataFrame → polars DataFrame with prefix-based renaming.

    Like `_to_pl_series`, pads the output to `input_length` when pandas-ta
    returns None. `prefix_map` is a dict of `pandas-ta-prefix → target name`
    (see `_smart_rename`). NaN values are mapped to polars nulls.
    """
    if df is None:
        target_cols = list(prefix_map.values()) if prefix_map else []
        if not target_cols:
            return pl.DataFrame()
        return pl.DataFrame({c: [None] * input_length for c in target_cols})
    if prefix_map:
        df = _smart_rename(df, prefix_map)
    out = pl.from_pandas(df)
    return out.with_columns([pl.col(c).fill_nan(None) for c in out.columns if out[c].dtype.is_float()])


# ─── Trend / moving averages ──────────────────────────────────────────


def sma(close: Any, length: int = 20) -> pl.Series:
    """Simple moving average."""
    s = _to_pd_series(close, "close")
    return _to_pl_series(ta.sma(s, length=length), f"sma_{length}", len(s))


def ema(close: Any, length: int = 20) -> pl.Series:
    """Exponential moving average (Wilder-style alpha = 2/(length+1))."""
    s = _to_pd_series(close, "close")
    return _to_pl_series(ta.ema(s, length=length), f"ema_{length}", len(s))


def wma(close: Any, length: int = 20) -> pl.Series:
    """Weighted moving average — linear weights, recent bars heaviest."""
    s = _to_pd_series(close, "close")
    return _to_pl_series(ta.wma(s, length=length), f"wma_{length}", len(s))


# ─── Momentum ─────────────────────────────────────────────────────────


def rsi(close: Any, length: int = 14) -> pl.Series:
    """Relative Strength Index (Wilder smoothing). Range 0–100."""
    s = _to_pd_series(close, "close")
    return _to_pl_series(ta.rsi(s, length=length), f"rsi_{length}", len(s))


def macd(
    close: Any,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pl.DataFrame:
    """Moving Average Convergence Divergence.

    Returns a DataFrame with columns: macd, macd_signal, macd_hist.
    """
    s = _to_pd_series(close, "close")
    out = ta.macd(s, fast=fast, slow=slow, signal=signal)
    # Prefix-match: MACDh_/MACDs_ are matched longest-first; MACD_ is the line itself.
    prefix_map = {
        "MACD_": "macd",
        "MACDh_": "macd_hist",
        "MACDs_": "macd_signal",
    }
    return _to_pl_frame(out, prefix_map, len(s))


def stoch(
    high: Any,
    low: Any,
    close: Any,
    k: int = 14,
    d: int = 3,
    smooth_k: int = 3,
) -> pl.DataFrame:
    """Stochastic oscillator. Returns DataFrame with stoch_k, stoch_d columns."""
    h, l, c = _to_pd_series(high, "high"), _to_pd_series(low, "low"), _to_pd_series(close, "close")
    out = ta.stoch(h, l, c, k=k, d=d, smooth_k=smooth_k)
    prefix_map = {"STOCHk_": "stoch_k", "STOCHd_": "stoch_d"}
    return _to_pl_frame(out, prefix_map, len(c))


# ─── Volatility ───────────────────────────────────────────────────────


def atr(high: Any, low: Any, close: Any, length: int = 14) -> pl.Series:
    """Average True Range — Wilder smoothing, absolute price units."""
    h, l, c = _to_pd_series(high, "high"), _to_pd_series(low, "low"), _to_pd_series(close, "close")
    return _to_pl_series(ta.atr(h, l, c, length=length), f"atr_{length}", len(c))


def bbands(close: Any, length: int = 20, std: float = 2.0) -> pl.DataFrame:
    """Bollinger Bands.

    Returns DataFrame with columns: bb_lower, bb_mid, bb_upper, bb_bandwidth, bb_percent.
    """
    s = _to_pd_series(close, "close")
    out = ta.bbands(s, length=length, std=std)
    prefix_map = {
        "BBL_": "bb_lower",
        "BBM_": "bb_mid",
        "BBU_": "bb_upper",
        "BBB_": "bb_bandwidth",
        "BBP_": "bb_percent",
    }
    return _to_pl_frame(out, prefix_map, len(s))


# ─── Volume ───────────────────────────────────────────────────────────


def obv(close: Any, volume: Any) -> pl.Series:
    """On-Balance Volume — cumulative volume signed by close direction."""
    c, v = _to_pd_series(close, "close"), _to_pd_series(volume, "volume")
    return _to_pl_series(ta.obv(c, v), "obv", len(c))


def vwap(high: Any, low: Any, close: Any, volume: Any) -> pl.Series:
    """Volume-Weighted Average Price — continuous (no session anchor).

    Computed directly: VWAP_t = Σ(typical_t × volume_t) / Σ(volume_t)
    where typical = (high + low + close) / 3. Crypto markets are 24/7,
    so we don't reset at a session boundary (which is what pandas-ta-classic
    does — it relies on the DatetimeIndex to identify session breaks).

    Computing directly removes our dependence on pandas-ta-classic's index
    semantics and gives the answer crypto traders actually want.
    """
    h = np.asarray(_to_pd_series(high, "high").values, dtype=float)
    l = np.asarray(_to_pd_series(low, "low").values, dtype=float)
    c = np.asarray(_to_pd_series(close, "close").values, dtype=float)
    v = np.asarray(_to_pd_series(volume, "volume").values, dtype=float)

    n = len(c)
    if n == 0:
        return pl.Series(name="vwap", values=[], dtype=pl.Float64)

    typical = (h + l + c) / 3.0
    cum_pv = np.cumsum(typical * v)
    cum_v = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap_arr = np.where(cum_v > 0, cum_pv / cum_v, np.nan)
    return pl.Series(name="vwap", values=vwap_arr).fill_nan(None)


# ─── Trend strength ───────────────────────────────────────────────────


def adx(high: Any, low: Any, close: Any, length: int = 14) -> pl.DataFrame:
    """Average Directional Index. Returns DataFrame with adx, dmp, dmn."""
    h, l, c = _to_pd_series(high, "high"), _to_pd_series(low, "low"), _to_pd_series(close, "close")
    out = ta.adx(h, l, c, length=length)
    prefix_map = {"ADX_": "adx", "DMP_": "dmp", "DMN_": "dmn"}
    return _to_pl_frame(out, prefix_map, len(c))


def supertrend(
    high: Any,
    low: Any,
    close: Any,
    length: int = 7,
    multiplier: float = 3.0,
) -> pl.DataFrame:
    """SuperTrend. Returns DataFrame with supertrend, supertrend_direction."""
    h, l, c = _to_pd_series(high, "high"), _to_pd_series(low, "low"), _to_pd_series(close, "close")
    out = ta.supertrend(h, l, c, length=length, multiplier=multiplier)
    prefix_map = {
        "SUPERT_": "supertrend",
        "SUPERTd_": "supertrend_direction",
        "SUPERTl_": "supertrend_long",
        "SUPERTs_": "supertrend_short",
    }
    return _to_pl_frame(out, prefix_map, len(c))


# ─── Statistics ───────────────────────────────────────────────────────


def zscore(close: Any, length: int = 30) -> pl.Series:
    """Rolling z-score: (x - rolling_mean) / rolling_std."""
    s = _to_pd_series(close, "close")
    return _to_pl_series(ta.zscore(s, length=length), f"zscore_{length}", len(s))
