"""Kelly sizing — single-asset and multi-asset with covariance.

The Kelly criterion (Kelly 1956) maximizes the expected log of wealth.
For a single bet with expected return μ and variance σ²:

    f* = μ / σ²

For a multi-asset portfolio with mean vector μ and covariance Σ:

    w* = Σ^-1 μ

Full Kelly is the geometric-growth-maximizing bet, but it's notoriously
sensitive to estimation error. In practice, EVERYONE uses fractional Kelly
(half, quarter, eighth) to compensate for uncertainty in μ and Σ.

Industry defaults:
  Half-Kelly (0.5) — AQR, Two Sigma practitioner standard
  Quarter-Kelly (0.25) — more risk-averse shops, retail-quant rule of thumb
  Eighth-Kelly (0.125) — very conservative

This module exposes both single and multi-asset versions with bounded
clamps so a 5σ outlier in μ doesn't blow up sizing.

References:
  Kelly, J. L. (1956). "A New Interpretation of Information Rate."
    Bell System Technical Journal 35, 917-926.
  Thorp, E. O. (2006). "The Kelly Criterion in Blackjack, Sports Betting,
    and the Stock Market."
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class KellyResult:
    """Kelly sizing decision with lineage."""

    weights: NDArray             # (N,) fractional weights (sum may exceed 1 with leverage)
    full_kelly_weights: NDArray  # before fraction multiplier
    fraction: float              # 0.5 = half-Kelly etc.
    gross_leverage: float        # sum(|weights|)
    expected_return: float       # μ' w
    expected_vol: float          # sqrt(w' Σ w)
    clamped: bool                # whether any clamping kicked in


# ─── Single-asset ─────────────────────────────────────────────────────


def kelly_fraction_single(
    expected_return_per_period: float,
    variance_per_period: float,
    fraction: float = 0.5,
    max_fraction: float = 1.0,
) -> float:
    """Single-asset Kelly fraction.

    Args:
      expected_return_per_period: μ (per period, NOT annualized — must match variance)
      variance_per_period:        σ² (per period)
      fraction:                   0.5 = half-Kelly (default)
      max_fraction:               hard cap on |output|. Default 1.0 = no leverage cap;
                                  raise to allow leveraged Kelly, lower for conservatism.

    Returns:
      Fraction of capital to allocate, signed (positive = long, negative = short).
      0 if variance ≤ 0 or expected return is non-finite.
    """
    if not np.isfinite(expected_return_per_period):
        return 0.0
    if variance_per_period <= 0 or not np.isfinite(variance_per_period):
        return 0.0
    if fraction < 0 or fraction > 1:
        raise ValueError(f"fraction must be in [0, 1]; got {fraction}")

    full_kelly = expected_return_per_period / variance_per_period
    scaled = fraction * full_kelly
    return float(np.clip(scaled, -max_fraction, max_fraction))


# ─── Multi-asset ──────────────────────────────────────────────────────


def kelly_weights_multi(
    expected_returns_per_period: NDArray | list[float],
    cov_matrix_per_period: NDArray,
    fraction: float = 0.5,
    max_gross_leverage: float = 3.0,
    max_single_weight: float = 0.5,
    regularization: float = 1e-8,
) -> KellyResult:
    """Multi-asset closed-form Kelly: w* = fraction · Σ^-1 μ.

    Args:
      expected_returns_per_period: (N,) μ vector
      cov_matrix_per_period:       (N, N) Σ matrix
      fraction:                    Kelly fraction (default 0.5 = half-Kelly)
      max_gross_leverage:          cap on sum(|w|). Scales the whole vector
                                   proportionally if exceeded.
      max_single_weight:           cap on |w_i| for any single asset
      regularization:              added to diagonal of Σ before inversion
                                   (prevents singular matrix errors when Σ is
                                   poorly conditioned — common with limited data)

    Returns:
      `KellyResult` with weights + diagnostics.
    """
    mu = np.asarray(expected_returns_per_period, dtype=np.float64).ravel()
    Sigma = np.atleast_2d(np.asarray(cov_matrix_per_period, dtype=np.float64))
    N = mu.size
    if Sigma.shape != (N, N):
        raise ValueError(
            f"cov_matrix shape {Sigma.shape} != (n, n) for n={N}"
        )
    if not np.isfinite(mu).all() or not np.isfinite(Sigma).all():
        return KellyResult(
            weights=np.zeros(N), full_kelly_weights=np.zeros(N),
            fraction=fraction, gross_leverage=0.0,
            expected_return=0.0, expected_vol=0.0, clamped=False,
        )

    # Regularize for numerical stability
    Sigma_reg = Sigma + regularization * np.eye(N)
    try:
        full_kelly = np.linalg.solve(Sigma_reg, mu)
    except np.linalg.LinAlgError:
        return KellyResult(
            weights=np.zeros(N), full_kelly_weights=np.zeros(N),
            fraction=fraction, gross_leverage=0.0,
            expected_return=0.0, expected_vol=0.0, clamped=False,
        )

    weights = fraction * full_kelly
    clamped = False

    # Clamp per-asset
    abs_weights = np.abs(weights)
    if abs_weights.max() > max_single_weight:
        scale = max_single_weight / abs_weights.max()
        weights = weights * scale
        clamped = True

    # Clamp gross leverage
    gross = float(np.abs(weights).sum())
    if gross > max_gross_leverage:
        scale = max_gross_leverage / gross
        weights = weights * scale
        gross = max_gross_leverage
        clamped = True

    expected_return = float(mu @ weights)
    expected_vol = float(np.sqrt(max(weights @ Sigma @ weights, 0.0)))

    return KellyResult(
        weights=weights,
        full_kelly_weights=full_kelly,
        fraction=fraction,
        gross_leverage=gross,
        expected_return=expected_return,
        expected_vol=expected_vol,
        clamped=clamped,
    )
