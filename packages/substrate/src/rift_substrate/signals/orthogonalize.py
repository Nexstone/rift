"""Signal orthogonalization — strip factor exposure from each signal.

For each signal s_i (column of a SignalScorePanel), regress its scores on
factor returns and replace with the residual:

    s_i_residual_t = s_i_t - α_i - Σ β_{i,k} * f_{k,t}

The residual is the part of the signal that's NOT explained by the factor
model — the "true" alpha component. Without this step, signals that look
predictive often turn out to be measuring factor exposure in disguise:
e.g., "momentum on volatile coins" is mostly just BTC beta + size factor.

Why this matters: combining factor-exposed signals via max-IR
double-counts the factor exposure. Each signal's IC against forward
returns is dominated by its factor loading, and you end up with a
"diversified" combined signal that's still ~80% BTC beta. The fix is
orthogonalizing FIRST, then combining the residuals.

Two methods:
  OLS (default):    regress each signal on factor returns, take residual.
                    Standard practice when factor returns are reasonably
                    stationary.
  Huber:            robust version. Use when signals have fat tails or
                    occasional large outliers that would skew OLS residuals.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.regression import huber_regression, ols_with_newey_west
from rift_substrate.signals.base import SignalScorePanel


@dataclass(frozen=True)
class OrthogonalizationResult:
    """Result of orthogonalizing a signal panel against a factor model.

    Attributes:
      orthogonalized_panel:  new SignalScorePanel with residual scores
      factor_loadings:       (K, F) per-signal factor loadings (β_{i,k})
      factor_names:          F factor names (aligned with columns of loadings)
      r_squared_per_signal:  (K,) how much of each signal was explained by factors
      method:                "OLS+NW" or "Huber+NW"
    """

    orthogonalized_panel: SignalScorePanel
    factor_loadings: NDArray
    factor_names: list[str]
    r_squared_per_signal: NDArray
    method: str = ""

    def loadings_dict(self) -> dict[str, dict[str, float]]:
        """Nested dict: {signal_name → {factor_name → β}}."""
        out: dict[str, dict[str, float]] = {}
        for i, sig in enumerate(self.orthogonalized_panel.signal_names):
            out[sig] = {
                fac: float(self.factor_loadings[i, k])
                for k, fac in enumerate(self.factor_names)
            }
        return out

    def summary(self) -> str:
        n_sig = len(self.orthogonalized_panel.signal_names)
        avg_r2 = float(np.nanmean(self.r_squared_per_signal))
        max_r2_idx = int(np.nanargmax(self.r_squared_per_signal))
        max_r2_sig = self.orthogonalized_panel.signal_names[max_r2_idx]
        lines = [
            f"OrthogonalizationResult  ({n_sig} signals, method={self.method})",
            "─" * 56,
            f"  Avg R² (signal vs factors):  {avg_r2:>+6.3f}",
            f"  Max R²:  {max_r2_sig} = {self.r_squared_per_signal[max_r2_idx]:.3f}",
            "       (high R² = signal was mostly factor exposure)",
        ]
        return "\n".join(lines)


def orthogonalize_signals(
    signal_panel: SignalScorePanel,
    factor_returns: NDArray,
    factor_names: list[str],
    use_robust: bool = False,
) -> OrthogonalizationResult:
    """Orthogonalize each signal in the panel against the factor returns.

    Args:
      signal_panel:    (T, K) panel of signal scores
      factor_returns:  (T, F) factor returns, ALIGNED with signal_panel.timestamps
      factor_names:    F factor names
      use_robust:      use Huber+NW instead of OLS+NW (recommended when
                       signals have fat tails)

    Returns:
      `OrthogonalizationResult` with:
        - new SignalScorePanel where each column is the residual after
          regressing that signal on factor returns
        - per-signal factor loadings (β_{i,k})
        - per-signal R² (how much of that signal was factor exposure)

    Edge cases:
      - signals with all-NaN columns → residuals stay NaN; loadings NaN; R² NaN
      - signals with too few valid observations → same
      - non-aligned shapes → ValueError
    """
    scores = np.asarray(signal_panel.scores, dtype=np.float64)
    F = np.atleast_2d(np.asarray(factor_returns, dtype=np.float64))
    T, K = scores.shape
    if F.shape[0] != T:
        raise ValueError(
            f"factor_returns rows ({F.shape[0]}) != signal_panel periods ({T})"
        )
    F_n_factors = F.shape[1]
    if F_n_factors != len(factor_names):
        raise ValueError(
            f"factor_returns columns ({F_n_factors}) != n_factor_names ({len(factor_names)})"
        )

    regress = huber_regression if use_robust else ols_with_newey_west

    residuals_out = np.full((T, K), np.nan, dtype=np.float64)
    loadings_out = np.full((K, F_n_factors), np.nan, dtype=np.float64)
    r2_out = np.full(K, np.nan, dtype=np.float64)
    methods: list[str] = []

    for j in range(K):
        sig = scores[:, j]
        mask = np.isfinite(sig) & np.all(np.isfinite(F), axis=1)
        if mask.sum() < F_n_factors + 5:
            # Too few valid rows to fit; leave NaN
            continue
        # Run regression
        result = regress(sig[mask], F[mask], add_constant=True)
        methods.append(result.method)
        # result.coef = [intercept, β_1, β_2, ...]; loadings are the slopes
        loadings_out[j, :] = result.coef[1:]
        r2_out[j] = float(result.r_squared) if np.isfinite(result.r_squared) else float("nan")
        # Residual = original signal - fitted value (intercept absorbed into residual? NO:
        # the residual is sig - X @ coef = sig - (intercept + Σ β·f). Including intercept.
        # We use result.residuals directly but they only cover the masked rows.
        residuals_full = np.full(T, np.nan)
        residuals_full[mask] = result.residuals
        residuals_out[:, j] = residuals_full

    method_label = methods[0] if methods else ("Huber+NW" if use_robust else "OLS+NW")

    new_panel = SignalScorePanel(
        scores=residuals_out,
        signal_names=list(signal_panel.signal_names),
        timestamps=signal_panel.timestamps.copy(),
    )
    return OrthogonalizationResult(
        orthogonalized_panel=new_panel,
        factor_loadings=loadings_out,
        factor_names=list(factor_names),
        r_squared_per_signal=r2_out,
        method=method_label,
    )
