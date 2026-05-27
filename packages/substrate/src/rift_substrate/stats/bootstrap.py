"""Stationary block bootstrap for time-series.

When you compute a confidence interval on a Sharpe ratio (or any time-series
statistic), the IID bootstrap is wrong — it destroys the serial dependence
in returns. The stationary block bootstrap of Politis & Romano (1994) draws
blocks of geometrically-distributed lengths from the original series, which
preserves the local dependence structure while still producing a valid
resampling distribution.

The block size matters. Too small and dependence isn't preserved; too large
and you don't get enough independent samples. Politis & White (2004) give
an automatic block-size selection rule based on the series' autocorrelation
structure.

Public:
  stationary_bootstrap(series, n_resamples, avg_block_size=None)
      → ndarray of shape (n_resamples, len(series))
  optimal_block_size(series) → int
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def _flatten_to_1d(series: NDArray | list | tuple) -> NDArray[np.float64]:
    arr = np.asarray(series, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"series must be 1-D; got shape {arr.shape}")
    if arr.size < 2:
        raise ValueError("series must have at least 2 observations")
    return arr


def optimal_block_size(series: NDArray | list | tuple, max_lag: int | None = None) -> int:
    """Politis & White (2004) optimal block size for stationary bootstrap.

    Conservative implementation. Real PW2004 uses a kernel-smoothed
    autocorrelation estimator with a data-dependent bandwidth. We use a
    simpler variant: find the smallest lag where |ACF| drops below the
    Bartlett 95% confidence band, then return a block size derived from it.

    Returns at least 2 and at most n // 4 (so blocks stay small enough
    that we get reasonable bootstrap diversity).
    """
    arr = _flatten_to_1d(series)
    n = arr.size
    upper_cap = max(2, n // 4)

    if max_lag is None:
        max_lag = min(n - 1, int(10 * np.log10(n)))

    # Sample ACF up to max_lag
    arr_centered = arr - arr.mean()
    var = float((arr_centered ** 2).sum() / n)
    if var <= 0:
        return 2

    # Bartlett 95% confidence band at large n
    threshold = 1.96 / np.sqrt(n)

    # Find first lag where |ACF| drops below threshold
    m_hat = 1
    for lag in range(1, max_lag + 1):
        acf = float((arr_centered[:-lag] * arr_centered[lag:]).sum() / (n * var))
        if abs(acf) < threshold:
            m_hat = lag
            break
    else:
        m_hat = max_lag

    # Politis-White heuristic: block_size ≈ (2 * m̂)^(1/3) * n^(1/3)
    block_size = int(np.ceil((2 * m_hat) ** (1 / 3) * n ** (1 / 3)))
    return int(np.clip(block_size, 2, upper_cap))


def stationary_bootstrap(
    series: NDArray | list | tuple,
    n_resamples: int = 1000,
    avg_block_size: int | None = None,
    seed: int | None = None,
) -> NDArray[np.float64]:
    """Politis & Romano (1994) stationary block bootstrap.

    Args:
      series:         1-D array of observations (returns, prices, etc.)
      n_resamples:    number of bootstrap samples to draw
      avg_block_size: expected block length (block lengths drawn from
                      Geometric(1/avg_block_size)). If None, auto-pick
                      via optimal_block_size().
      seed:           RNG seed for reproducibility

    Returns:
      ndarray of shape (n_resamples, len(series)) — each row is one
      resampled series of the same length as the input.

    Algorithm:
      Each resample is constructed by concatenating blocks. Start a block
      at a random position; the block length L is Geometric(p=1/L̄). Wrap
      indices modulo n (circular series). Repeat until the resample is
      length n.
    """
    arr = _flatten_to_1d(series)
    n = arr.size

    if avg_block_size is None:
        avg_block_size = optimal_block_size(arr)
    if avg_block_size < 1:
        raise ValueError(f"avg_block_size must be >= 1; got {avg_block_size}")

    rng = np.random.default_rng(seed)
    p = 1.0 / avg_block_size

    out = np.empty((n_resamples, n), dtype=np.float64)
    for r in range(n_resamples):
        idx = np.empty(n, dtype=np.int64)
        i = 0
        while i < n:
            start = rng.integers(0, n)
            # Geometric block length (>=1)
            block_len = max(1, rng.geometric(p))
            end = min(i + block_len, n)
            take = end - i
            # Wrap around circularly
            positions = (start + np.arange(take)) % n
            idx[i:end] = positions
            i = end
        out[r] = arr[idx]

    return out
