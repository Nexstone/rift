"""White's Reality Check — White (2000).

When you run N strategies and pick the best, the observed best
out-performance vs. a baseline is biased upward by selection. White's
Reality Check provides a bootstrap-based p-value for the hypothesis
"the BEST strategy is significantly better than the baseline" while
properly accounting for the multiple-comparison nature of the question.

Algorithm:
  1. Compute observed test statistic: f_max = max over k of mean(f_k)
     where f_k is the per-period out-performance of strategy k vs baseline.
  2. Bootstrap N times: resample the strategy-vs-baseline time series
     (preserving cross-strategy contemporaneous structure) using stationary
     block bootstrap.
  3. For each bootstrap b: compute V_b = max over k of (mean(f_k^b) - mean(f_k)).
     This is the studentized max, centered at the in-sample mean.
  4. p-value = (1 + # of bootstraps where V_b > f_max) / (1 + n_bootstrap)

A small p-value means the best strategy is unlikely to be just luck.

Reference:
  White, H. (2000). "A Reality Check for Data Snooping."
  Econometrica, 68(5), 1097-1126.

Hansen's SPA (Superior Predictive Ability) test is a power improvement
on White's RC but more complex. We implement White's first; SPA can be
added later if needed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from rift_substrate.stats.bootstrap import optimal_block_size


@dataclass(frozen=True)
class RealityCheckResult:
    """Outcome of White's Reality Check.

    p_value answers: "what's the probability the best strategy's
    out-performance is just luck?"
    """
    n_strategies: int
    n_observations: int
    best_strategy_idx: int
    best_mean_outperformance: float    # per-period
    p_value: float
    bootstrap_max_distribution: NDArray  # for plotting / diagnostics
    block_size: int

    @property
    def significant_at_5pct(self) -> bool:
        return self.p_value < 0.05

    @property
    def significant_at_1pct(self) -> bool:
        return self.p_value < 0.01


def whites_reality_check(
    strategy_returns: list[NDArray] | NDArray,
    baseline_returns: NDArray,
    n_bootstrap: int = 1000,
    block_size: int | None = None,
    seed: int | None = None,
) -> RealityCheckResult:
    """Tests whether the best of N strategies significantly out-performs
    a baseline, with proper multiple-comparison adjustment.

    Args:
      strategy_returns:  list of K arrays (one per strategy), each of
                         length T (per-period returns)
                         OR a 2D array of shape (T, K)
      baseline_returns:  array of length T (per-period baseline returns —
                         e.g., HODL, zero, market index)
      n_bootstrap:       number of bootstrap resamples (default 1000)
      block_size:        avg block length; auto-pick if None
      seed:              RNG seed

    Returns:
      RealityCheckResult with p_value and metadata.

    Notes:
      - All series must have the same length T
      - Out-performance is computed per-strategy: f_k[t] = strategy_k[t] - baseline[t]
      - Bootstrap preserves cross-strategy contemporaneous structure
        (we resample the time index, not each series independently)
    """
    # Coerce to a 2D array (T, K)
    if isinstance(strategy_returns, list):
        T = len(strategy_returns[0])
        K = len(strategy_returns)
        strat_matrix = np.empty((T, K), dtype=np.float64)
        for k, s in enumerate(strategy_returns):
            arr = np.asarray(s, dtype=np.float64)
            if arr.size != T:
                raise ValueError(
                    f"strategy {k} has {arr.size} observations; expected {T}"
                )
            strat_matrix[:, k] = arr
    else:
        strat_matrix = np.asarray(strategy_returns, dtype=np.float64)
        if strat_matrix.ndim != 2:
            raise ValueError(f"strategy_returns must be 2D (T, K); got shape {strat_matrix.shape}")
        T, K = strat_matrix.shape

    baseline = np.asarray(baseline_returns, dtype=np.float64)
    if baseline.size != T:
        raise ValueError(f"baseline has {baseline.size} observations; expected {T}")

    if K < 1:
        raise ValueError("need at least 1 strategy")
    if T < 2:
        raise ValueError("need at least 2 observations per strategy")

    # Out-performance matrix: f[t, k] = strategy_k[t] - baseline[t]
    f = strat_matrix - baseline[:, None]

    # In-sample best
    f_means = f.mean(axis=0)
    best_idx = int(np.argmax(f_means))
    f_max = float(f_means[best_idx])

    # Bootstrap
    if block_size is None:
        # Use the most informative single strategy for block-size selection
        block_size = optimal_block_size(f[:, best_idx])

    rng = np.random.default_rng(seed)
    p = 1.0 / block_size

    # Centered statistic distribution: V_b = max_k (f_b_k_mean - f_k_mean)
    v_distribution = np.empty(n_bootstrap, dtype=np.float64)
    for b in range(n_bootstrap):
        # Stationary block resample of TIME INDICES (preserves cross-strategy structure)
        idx = np.empty(T, dtype=np.int64)
        i = 0
        while i < T:
            start = rng.integers(0, T)
            block_len = max(1, rng.geometric(p))
            end = min(i + block_len, T)
            take = end - i
            positions = (start + np.arange(take)) % T
            idx[i:end] = positions
            i = end

        # Resampled out-performance matrix
        f_b = f[idx]
        # Centered max
        v_distribution[b] = float(np.max(f_b.mean(axis=0) - f_means))

    # p-value: fraction of bootstrap V_b >= observed centered max (which is 0)
    # White's RC tests if f_max significantly exceeds the bootstrap distribution
    # of MAX centered means; we ask P(V_b >= f_max)
    p_value = float((1 + np.sum(v_distribution >= f_max)) / (1 + n_bootstrap))

    return RealityCheckResult(
        n_strategies=K,
        n_observations=T,
        best_strategy_idx=best_idx,
        best_mean_outperformance=f_max,
        p_value=p_value,
        bootstrap_max_distribution=v_distribution,
        block_size=int(block_size),
    )
