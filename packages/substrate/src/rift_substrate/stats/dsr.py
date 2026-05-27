"""Deflated Sharpe Ratio — Bailey & López de Prado (2014).

When you run a parameter sweep and report the best Sharpe, that Sharpe
is upwardly biased. You searched a hypothesis space; the best result was
selected from many trials; the magnitude of the bias depends on how many
trials and how variable the trial Sharpes are.

DSR corrects for this selection bias. It asks:

    P(true Sharpe > 0 | observed best Sharpe, N trials, variance of trial Sharpes,
                       skew, kurtosis, observation count)

The deflation is done by computing an "expected maximum" Sharpe ratio
under the null hypothesis of zero true Sharpe across all trials, and
asking whether the OBSERVED maximum is significantly larger than that
expected maximum.

Formula:

    SR_0 = sqrt(variance_of_trial_sharpes) * (
        (1 - γ) * Φ⁻¹(1 - 1/N) + γ * Φ⁻¹(1 - 1/(N*e))
    )

Where γ is the Euler-Mascheroni constant (≈ 0.5772).

Then:

    DSR = PSR(observed_best_sharpe, threshold = SR_0, ...)

Reference:
  Bailey, D. H. & López de Prado, M. M. (2014). "The Deflated Sharpe
  Ratio: Correcting for Selection Bias, Backtest Overfitting, and
  Non-Normality." Journal of Portfolio Management, 40(5), 94-107.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm

from rift_substrate.stats.psr import probabilistic_sharpe_ratio


EULER_MASCHERONI = 0.5772156649015329


def expected_max_sharpe_under_null(
    n_trials: int,
    variance_of_trial_sharpes: float,
) -> float:
    """Expected value of the maximum Sharpe across N trials when the true
    Sharpe is 0 (the deflation threshold).

    Derived from extreme-value theory applied to standard normal samples.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1; got {n_trials}")
    if variance_of_trial_sharpes < 0:
        raise ValueError(f"variance must be >= 0; got {variance_of_trial_sharpes}")

    sd = np.sqrt(variance_of_trial_sharpes)
    if n_trials == 1:
        return 0.0
    # Bailey-López de Prado eq. (5)
    term1 = (1.0 - EULER_MASCHERONI) * norm.ppf(1.0 - 1.0 / n_trials)
    term2 = EULER_MASCHERONI * norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    return float(sd * (term1 + term2))


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_observations: int,
    n_trials: int,
    variance_of_trial_sharpes: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """P(true Sharpe > 0 | observed best Sharpe from N trials).

    Args:
      observed_sharpe:           per-period Sharpe of the best trial
      n_observations:            number of return observations
      n_trials:                  number of trials searched (sweep size)
      variance_of_trial_sharpes: sample variance of Sharpe ratios across
                                 the N trials
      skew:                      sample skewness of the best trial's returns
      kurtosis:                  sample kurtosis of the best trial's returns

    Returns:
      Probability in [0, 1] that the observed best Sharpe reflects a real
      edge after correcting for selection bias.

    Edge cases:
      - n_trials = 1 reduces to PSR with threshold = 0 (no selection bias)
      - variance_of_trial_sharpes = 0 means all trials had identical
        Sharpes (no diversity to deflate against); reduces to PSR(threshold=0)
    """
    if n_observations < 2:
        return float("nan")

    threshold = expected_max_sharpe_under_null(n_trials, variance_of_trial_sharpes)
    return probabilistic_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        n_observations=n_observations,
        threshold=threshold,
        skew=skew,
        kurtosis=kurtosis,
    )


def deflated_sharpe_ratio_from_trial_results(
    trial_sharpes: list[float] | np.ndarray,
    n_observations_per_trial: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Convenience wrapper: takes the full list of trial Sharpes.

    Computes:
      observed_sharpe           = max(trial_sharpes)
      n_trials                  = len(trial_sharpes)
      variance_of_trial_sharpes = sample variance of trial_sharpes

    Assumes all trials have the same observation count and (skew, kurtosis)
    structure.
    """
    arr = np.asarray(trial_sharpes, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size < 1:
        return float("nan")

    observed = float(arr.max())
    n_trials = int(arr.size)
    if n_trials < 2:
        var = 0.0
    else:
        var = float(np.var(arr, ddof=1))

    return deflated_sharpe_ratio(
        observed_sharpe=observed,
        n_observations=n_observations_per_trial,
        n_trials=n_trials,
        variance_of_trial_sharpes=var,
        skew=skew,
        kurtosis=kurtosis,
    )
