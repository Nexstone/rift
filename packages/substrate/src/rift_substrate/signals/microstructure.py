"""Microstructure-derived signals — primitives over the L2-derived candle columns.

Compose with `rift_substrate.decay.compute_ic_curve` and
`rift_substrate.decay.estimate_half_life` to answer:

  "Does order-book imbalance predict next-period returns? At what horizon
   does its IC peak? When does it decay?"

These primitives operate on the L2-aggregated candle columns produced by
`rift_data.s3.l2_books.sync_l2_candles`:

  - `bid_depth_top10_usd`        — total resting bid size in top 10 levels
  - `ask_depth_top10_usd`        — total resting ask size
  - `order_book_imbalance`       — (bid - ask) / (bid + ask), range [-1, +1]
  - `mean_spread_bps`            — average spread across the bar
  - `max_bid_wall_usd`,
    `max_ask_wall_usd`           — largest single-level resting size

Each primitive is a NumPy-only function returning a feature time series the
caller can pass through `compute_ic_curve(signal, forward_returns, horizons)`.

Reference:
  Cont, Stoikov, Talreja (2010). "A stochastic model for order book dynamics."
  Cartea, Jaimungal, Penalva (2015). "Algorithmic and High-Frequency Trading."
    Ch. 4 on order-flow imbalance and short-horizon predictability.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def book_imbalance(
    bid_depth: NDArray | list[float],
    ask_depth: NDArray | list[float],
) -> NDArray:
    """Compute book imbalance ratio from raw bid/ask depth.

    `imbalance = (bid - ask) / (bid + ask)`

    Returns values in [-1, +1]:
      +1  → bid depth dominates (buying pressure observable)
       0  → balanced book
      -1  → ask depth dominates (selling pressure observable)

    Inputs must have the same length. NaN propagates. Where both sides
    are zero (no book observed), returns NaN for that timestep.
    """
    bid = np.asarray(bid_depth, dtype=np.float64)
    ask = np.asarray(ask_depth, dtype=np.float64)
    if bid.shape != ask.shape:
        raise ValueError(f"bid/ask shape mismatch: {bid.shape} vs {ask.shape}")
    total = bid + ask
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(total > 0, (bid - ask) / total, np.nan)
    return out


def book_imbalance_zscore(
    imbalance: NDArray | list[float],
    window: int = 24,
) -> NDArray:
    """Rolling z-score of the imbalance series.

    Useful for "how unusual is the current imbalance vs the last N bars?"
    Returns NaN for the first `window-1` positions (insufficient history).

    Args:
      imbalance: series in [-1, +1] from `book_imbalance()` or candle column
      window:    rolling window size in bars (default 24 = 1 day at 1h)
    """
    arr = np.asarray(imbalance, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"imbalance must be 1-D; got shape {arr.shape}")
    if window < 2:
        raise ValueError(f"window must be >= 2; got {window}")
    n = arr.size
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(window - 1, n):
        win = arr[i - window + 1 : i + 1]
        win = win[np.isfinite(win)]
        if win.size < 2:
            continue
        std = win.std(ddof=1)
        if std == 0:
            continue
        out[i] = (arr[i] - win.mean()) / std
    return out


def wall_intensity(
    max_wall_usd: NDArray | list[float],
    depth_usd: NDArray | list[float],
) -> NDArray:
    """Fraction of one-sided depth concentrated in the single biggest level.

    `intensity = max_wall_usd / depth_usd`

    Returns values in [0, 1]:
      high → one large resting order ("wall") dominates the depth — possible
             support/resistance level OR a spoof
      low  → depth is spread evenly across many levels — no single dominant
             level

    Used to detect "wall" structures: combined with imbalance, a thick wall
    on the opposite side of the trade direction is a contrarian signal.
    """
    wall = np.asarray(max_wall_usd, dtype=np.float64)
    depth = np.asarray(depth_usd, dtype=np.float64)
    if wall.shape != depth.shape:
        raise ValueError(f"wall/depth shape mismatch: {wall.shape} vs {depth.shape}")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(depth > 0, wall / depth, np.nan)
    return out


def spread_pressure(
    spread_bps: NDArray | list[float],
    typical_spread_bps: float = 5.0,
) -> NDArray:
    """Normalized spread series — multiples of the typical spread.

    `pressure = spread_bps / typical_spread_bps`

    Values > 1 indicate stress / one-sided market making. The "typical"
    spread is asset-dependent — pass a coin-specific calibration if you
    have one (e.g., 5 bps for BTC, 10 for ETH, 50+ for thin alts).
    """
    arr = np.asarray(spread_bps, dtype=np.float64)
    if typical_spread_bps <= 0:
        raise ValueError(f"typical_spread_bps must be > 0; got {typical_spread_bps}")
    return arr / typical_spread_bps
