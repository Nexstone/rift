"""Technical indicators — polars-native wrappers around pandas-ta-classic.

Why this exists: quant-curious users expect RSI, MACD, Bollinger Bands,
ATR, etc. to be one import away. Building them ad-hoc inside strategies
is error-prone (off-by-one EMA seeds, wrong Wilder smoothing, etc.).

Two ways to use this module:

  # Curated wrappers — typed, polars-native, aligned to input length:
  from rift_substrate.indicators import rsi, ema, macd, bbands, atr
  signal = rsi(df["close"], length=14)

  # Power-user escape hatch — full pandas-ta-classic surface:
  from rift_substrate.indicators import ta
  signal = ta.kama(df["close"].to_pandas(), length=14)

The curated wrappers accept polars Series, pandas Series, numpy arrays,
or python lists. Output is always a polars Series with a descriptive name
(e.g. "rsi_14") and is left-padded with nulls for the warm-up window so
the result aligns 1-to-1 with the input.
"""

from __future__ import annotations

from rift_substrate.indicators.core import (
    adx,
    atr,
    bbands,
    ema,
    macd,
    obv,
    rsi,
    sma,
    stoch,
    supertrend,
    vwap,
    wma,
    zscore,
)

# Power-user escape hatch — expose the full pandas-ta-classic library so
# any indicator we haven't wrapped is still reachable as `indicators.ta.<x>`.
import pandas_ta_classic as ta  # noqa: E402

__all__ = [
    "adx",
    "atr",
    "bbands",
    "ema",
    "macd",
    "obv",
    "rsi",
    "sma",
    "stoch",
    "supertrend",
    "ta",
    "vwap",
    "wma",
    "zscore",
]
