"""Probabilistic Sharpe Ratio — Bailey & López de Prado (2012).

Given an observed Sharpe ratio computed from N observations, PSR answers:

    P(true Sharpe > threshold | observed Sharpe, N, skew, kurtosis)

The naive interpretation of a Sharpe ratio assumes IID normal returns.
Real return streams have skew and excess kurtosis; PSR adjusts the
significance of the observed Sharpe accordingly.

Formula (from the paper):

    PSR(SR*) = Φ( (SR_obs - SR*) * sqrt(N - 1) /
                  sqrt(1 - γ_3 * SR_obs + ((γ_4 - 1) / 4) * SR_obs^2) )

Where:
  SR_obs = observed Sharpe (annualized or per-period — must match SR*)
  SR*    = threshold to test against (e.g., 0)
  N      = number of observations
  γ_3    = skewness of returns
  γ_4    = kurtosis of returns (NOT excess; normal = 3)
  Φ      = standard normal CDF

For periodic Sharpe (e.g., daily observed Sharpe of 0.05), pass that
directly. For an annualized Sharpe, divide by sqrt(periods_per_year)
first or use the convenience wrapper.

Reference:
  Bailey, D. H. & López de Prado, M. M. (2012). "The Sharpe Ratio
  Efficient Frontier." Journal of Risk, 15(2), 13-44.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm


def probabilistic_sharpe_ratio(
    observed_sharpe: float,
    n_observations: int,
    threshold: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """P(true Sharpe > threshold | observed, n, distribution moments).

    Args:
      observed_sharpe:  per-period observed Sharpe (NOT annualized)
      n_observations:   number of return observations
      threshold:        per-period Sharpe to compare against (default 0)
      skew:             sample skewness of returns (default 0 = symmetric)
      kurtosis:         sample kurtosis of returns (default 3 = normal)

    Returns:
      Probability in [0, 1].

    Note on annualization:
      If your observed Sharpe is annualized, convert to per-period before
      passing:  per_period = annualized / sqrt(periods_per_year).
      The threshold should also be per-period.

    Edge cases:
      - n < 2 returns NaN (can't compute)
      - degenerate variance (denominator ≤ 0) returns NaN
      - extreme inputs produce probabilities asymptotically near 0 or 1
    """
    if n_observations < 2:
        return float("nan")

    # Denominator: standard error of the observed Sharpe under the
    # Bailey-López de Prado correction for skew and kurtosis.
    variance_term = 1.0 - skew * observed_sharpe + ((kurtosis - 1.0) / 4.0) * observed_sharpe ** 2
    if variance_term <= 0:
        return float("nan")

    z = (observed_sharpe - threshold) * np.sqrt(n_observations - 1) / np.sqrt(variance_term)
    return float(norm.cdf(z))


def probabilistic_sharpe_ratio_annualized(
    observed_sharpe_annualized: float,
    n_observations: int,
    periods_per_year: float,
    threshold_annualized: float = 0.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Convenience wrapper: takes annualized Sharpe + threshold.

    Internally converts both to per-period before applying PSR formula.
    """
    sqrt_ppy = np.sqrt(periods_per_year)
    observed_per_period = observed_sharpe_annualized / sqrt_ppy
    threshold_per_period = threshold_annualized / sqrt_ppy
    return probabilistic_sharpe_ratio(
        observed_sharpe=observed_per_period,
        n_observations=n_observations,
        threshold=threshold_per_period,
        skew=skew,
        kurtosis=kurtosis,
    )
