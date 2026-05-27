"""Max-IR signal combiner — closed-form Σ^-1·IC with Ledoit-Wolf shrinkage.

The optimization problem: find weights w that maximize the information
ratio of the combined signal,

    IR(w) = w'·IC / sqrt(w'·Σ_s·w)

where:
  IC[k]  = correlation of signal k with forward returns (information coefficient)
  Σ_s    = covariance matrix of signal scores (shrunk via Ledoit-Wolf)

Closed-form solution (when no constraints): w ∝ Σ_s^-1 · IC. We normalize
to a chosen leverage target (default: gross leverage = 1). With constraints
(per-signal caps, gross/net leverage, factor-neutrality on a separate axis),
we delegate to the Phase 2c `MeanVarianceOptimizer`.

This is the Grinold "fundamental law of active management" framework:
  IR = IC × sqrt(breadth)
The max-IR combiner maximizes the effective breadth across correlated
signals — diversifying signal noise without naively averaging.

Usage:

    combiner = MaxIRCombiner(use_shrinkage=True)
    combiner.fit(orthogonalized_panel, forward_returns)

    # Apply weights to fresh scores
    combined_score = combiner.combine(latest_scores)

    # Inspect the fit
    print(combiner.weights_)
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.covariance import LedoitWolfCovariance, SampleCovariance
from rift_substrate.risk.optimizer import (
    MeanVarianceOptimizer,
    OptimizationConstraints,
)
from rift_substrate.signals.base import (
    InformationCoefficients,
    MaxIRWeights,
    SignalScorePanel,
)


def information_coefficients(
    signals: NDArray | SignalScorePanel,
    forward_returns: NDArray | list[float],
    method: str = "pearson",
    signal_names: list[str] | None = None,
) -> InformationCoefficients:
    """Compute per-signal information coefficients vs. forward returns.

    IC_k = corr(signal_k_t, forward_return_t)

    Args:
      signals:         either an NDArray (T, K) or a SignalScorePanel
      forward_returns: (T,) returns aligned with the signal panel
      method:          "pearson" or "spearman" (rank correlation)
      signal_names:    K names; required if `signals` is an NDArray

    Returns:
      `InformationCoefficients` with one IC per signal.

    Edge cases:
      - Constant-valued signals (no variance) → NaN IC
      - All-NaN signal columns → NaN IC
      - Length mismatch raises
    """
    if isinstance(signals, SignalScorePanel):
        S = signals.scores
        names = list(signals.signal_names)
    else:
        S = np.atleast_2d(np.asarray(signals, dtype=np.float64))
        names = (
            list(signal_names)
            if signal_names is not None
            else [f"signal_{i}" for i in range(S.shape[1])]
        )
    fr = np.asarray(forward_returns, dtype=np.float64).ravel()
    if fr.size != S.shape[0]:
        raise ValueError(
            f"forward_returns length ({fr.size}) != signal panel periods ({S.shape[0]})"
        )

    T, K = S.shape
    ics = np.full(K, np.nan, dtype=np.float64)

    if method == "spearman":
        # Use scipy's spearmanr for ranks
        from scipy.stats import spearmanr
        for j in range(K):
            col = S[:, j]
            mask = np.isfinite(col) & np.isfinite(fr)
            if mask.sum() < 5:
                continue
            try:
                rho, _ = spearmanr(col[mask], fr[mask])
                ics[j] = float(rho) if np.isfinite(rho) else float("nan")
            except Exception:
                continue
    else:
        # Pearson
        for j in range(K):
            col = S[:, j]
            mask = np.isfinite(col) & np.isfinite(fr)
            if mask.sum() < 5:
                continue
            x = col[mask]
            y = fr[mask]
            if x.std(ddof=1) <= 0 or y.std(ddof=1) <= 0:
                continue
            ics[j] = float(np.corrcoef(x, y)[0, 1])

    valid_obs = int(np.isfinite(fr).sum())
    return InformationCoefficients(
        values=ics,
        signal_names=names,
        n_observations=valid_obs,
        method=method,
    )


class MaxIRCombiner:
    """Combine signals into a single composite that maximizes information ratio.

    Fit on (orthogonalized signals, forward returns). The closed-form
    solution is `w ∝ Σ_s^-1 · IC`, where Σ_s is the signal covariance
    (shrunk by default) and IC is the per-signal information coefficient.

    For constrained combining (per-signal caps, gross leverage limits,
    factor-neutrality), pass `constraints` and the combiner delegates
    to the Phase 2c `MeanVarianceOptimizer`.

    Args:
      use_shrinkage:    Ledoit-Wolf shrinkage on the signal covariance (default True)
      constraints:      OptimizationConstraints; if None, closed-form is used
      max_gross_leverage_closed_form: normalize closed-form weights to this gross
                        leverage (default 1.0). Ignored when constraints are passed.
      ic_method:        "pearson" or "spearman" for IC computation
      ic_floor:         signals with |IC| below this are zeroed out before solving
                        (default 0.0 = no floor; set ~0.01 to filter noise signals)

    After fitting, the result is in `self.fit_result` (a MaxIRWeights instance).
    """

    def __init__(
        self,
        use_shrinkage: bool = True,
        constraints: OptimizationConstraints | None = None,
        max_gross_leverage_closed_form: float = 1.0,
        ic_method: str = "pearson",
        ic_floor: float = 0.0,
    ):
        self.use_shrinkage = use_shrinkage
        self.constraints = constraints
        self.max_gross_leverage_closed_form = max_gross_leverage_closed_form
        self.ic_method = ic_method
        self.ic_floor = ic_floor
        self.fit_result: MaxIRWeights | None = None

    # ─── Fit ──────────────────────────────────────────────────────────

    def fit(
        self,
        signals: NDArray | SignalScorePanel,
        forward_returns: NDArray | Sequence[float],
        periods_per_year: float = 365.0,
    ) -> "MaxIRCombiner":
        """Fit combiner weights.

        Args:
          signals:           (T, K) panel or SignalScorePanel
          forward_returns:   (T,) realized returns to predict
          periods_per_year:  annualization for in-sample IR
        """
        if isinstance(signals, SignalScorePanel):
            S = signals.scores
            names = list(signals.signal_names)
        else:
            S = np.atleast_2d(np.asarray(signals, dtype=np.float64))
            names = [f"signal_{i}" for i in range(S.shape[1])]

        fr = np.asarray(forward_returns, dtype=np.float64).ravel()
        if fr.size != S.shape[0]:
            raise ValueError(
                f"forward_returns length ({fr.size}) != panel periods ({S.shape[0]})"
            )

        # 1. ICs (per-signal correlation with forward returns)
        ic = information_coefficients(S, fr, method=self.ic_method, signal_names=names)
        ic_values = ic.values.copy()

        # Filter low-IC signals
        if self.ic_floor > 0:
            ic_values = np.where(np.abs(ic_values) < self.ic_floor, 0.0, ic_values)

        # Replace NaN ICs with 0 (signal has no usable info)
        ic_values = np.where(np.isfinite(ic_values), ic_values, 0.0)

        # 2. Signal covariance
        # Drop rows with any NaN signals (LW formula needs clean inputs)
        clean_mask = np.all(np.isfinite(S), axis=1)
        S_clean = S[clean_mask]
        if S_clean.shape[0] < S.shape[1] + 5:
            # Too few clean rows — fall back to zero weights
            return self._fail_fit(names, S.shape[0])

        if self.use_shrinkage:
            cov_est = LedoitWolfCovariance().fit(S_clean, asset_names=names)
        else:
            cov_est = SampleCovariance().fit(S_clean, asset_names=names)

        # 3. Compute weights
        if self.constraints is not None:
            opt = MeanVarianceOptimizer()
            res = opt.optimize(
                expected_returns=ic_values,
                cov_matrix=cov_est.cov,
                constraints=self.constraints,
            )
            weights = res.weights
            converged = res.converged
            method = "mv_optimizer"
        else:
            # Closed-form: w ∝ Σ^-1 · IC
            try:
                inv_cov_ic = np.linalg.solve(
                    cov_est.cov + 1e-10 * np.eye(cov_est.cov.shape[0]),
                    ic_values,
                )
            except np.linalg.LinAlgError:
                return self._fail_fit(names, S.shape[0])
            # Normalize to target gross leverage
            gross_raw = float(np.abs(inv_cov_ic).sum())
            if gross_raw > 0:
                weights = inv_cov_ic * (self.max_gross_leverage_closed_form / gross_raw)
            else:
                weights = inv_cov_ic
            converged = True
            method = "closed_form"

        # 4. In-sample IC and IR of the combined signal
        # combined_score_t = Σ w_k · s_{k,t}
        # Matmul warnings can leak through from prior numerical ops; harmless here.
        with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
            combined_signal_scores = S_clean @ weights
        fr_clean = fr[clean_mask]
        if combined_signal_scores.std(ddof=1) > 0 and fr_clean.std(ddof=1) > 0:
            in_sample_ic = float(np.corrcoef(combined_signal_scores, fr_clean)[0, 1])
        else:
            in_sample_ic = float("nan")
        # Sharpe of the combined signal × forward return product (per-period PnL proxy)
        # This is a rough in-sample IR. Better: walk-forward, but that's the caller's job.
        product = combined_signal_scores * fr_clean
        if product.std(ddof=1) > 0:
            per_period_ir = float(product.mean() / product.std(ddof=1))
            in_sample_ir = per_period_ir * np.sqrt(periods_per_year)
        else:
            in_sample_ir = float("nan")

        self.fit_result = MaxIRWeights(
            weights=weights,
            signal_names=names,
            gross_leverage=float(np.abs(weights).sum()),
            net_leverage=float(weights.sum()),
            in_sample_ic=in_sample_ic,
            in_sample_ir=in_sample_ir,
            n_observations=int(S_clean.shape[0]),
            method=method,
            converged=converged,
            shrinkage_lambda=(
                cov_est.shrinkage_lambda if self.use_shrinkage else float("nan")
            ),
        )
        return self

    # ─── Apply ────────────────────────────────────────────────────────

    def combine(self, latest_scores: NDArray | Sequence[float]) -> float:
        """Apply fitted weights to a single timestamp's signal scores."""
        if self.fit_result is None:
            raise RuntimeError("MaxIRCombiner must be fit before calling combine()")
        x = np.asarray(latest_scores, dtype=np.float64).ravel()
        if x.size != self.fit_result.weights.size:
            raise ValueError(
                f"latest_scores size ({x.size}) != n_signals ({self.fit_result.weights.size})"
            )
        # NaN scores → contribute zero (graceful degradation)
        x_clean = np.where(np.isfinite(x), x, 0.0)
        return float(self.fit_result.weights @ x_clean)

    def combine_batch(self, panel: NDArray | SignalScorePanel) -> NDArray:
        """Apply weights to a full panel → (T,) combined scores."""
        if self.fit_result is None:
            raise RuntimeError("MaxIRCombiner must be fit before calling combine_batch()")
        S = panel.scores if isinstance(panel, SignalScorePanel) else np.asarray(panel, dtype=np.float64)
        if S.shape[1] != self.fit_result.weights.size:
            raise ValueError(
                f"panel signal count ({S.shape[1]}) != n_signals ({self.fit_result.weights.size})"
            )
        S_clean = np.where(np.isfinite(S), S, 0.0)
        with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
            return S_clean @ self.fit_result.weights

    @property
    def weights_(self) -> NDArray:
        """Fitted weight vector."""
        if self.fit_result is None:
            raise RuntimeError("MaxIRCombiner not fit yet")
        return self.fit_result.weights

    # ─── Helpers ──────────────────────────────────────────────────────

    def _fail_fit(self, names: list[str], n_periods: int) -> "MaxIRCombiner":
        self.fit_result = MaxIRWeights(
            weights=np.zeros(len(names)),
            signal_names=names,
            n_observations=n_periods,
            method="insufficient_data",
            converged=False,
        )
        return self
