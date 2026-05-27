"""Mean-variance optimizer with constraints.

Solves the Markowitz quadratic program:

    maximize    w' μ - (λ/2) w' Σ w - turnover_penalty · ||w - w_prev||_1
    subject to  -bounds ≤ w ≤ bounds                       (per-asset cap)
                |sum(|w|)| ≤ max_gross_leverage             (gross cap)
                |sum(w)| ≤ max_net_leverage                 (net cap)
                F' w = 0  (optionally, for selected factor exposures)
                                                            (factor-neutral)

where:
  μ              — expected returns vector (N,)
  Σ              — covariance matrix (N, N)
  λ              — risk aversion (higher = more conservative). Default 1.0.
  w_prev         — previous weights (for turnover penalty). Default 0.
  bounds         — per-asset weight cap
  F              — factor exposure matrix (N, k_factors) — zero out specified exposures

For max-IR sizing of orthogonalized signals (Phase 3 use case), pass:
  μ = signal residual returns after factor orthogonalization
  Σ = factor-implied residual covariance from the factor model

Backend: scipy.optimize.minimize with SLSQP. Fast and reliable for N up
to ~50 assets. For larger N or more complex constraints, cvxpy is the
next step — but we don't ship that dep yet.

The optimizer returns weights summing to a leverage in [-net, +net] with
|w_i| ≤ single_position cap. Setting bounds and leverage to ∞ gives the
unconstrained mean-variance tangency portfolio (equivalent to multi-asset
Kelly with λ=1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize


@dataclass(frozen=True)
class OptimizationConstraints:
    """Constraints applied to the MV optimizer."""

    max_gross_leverage: float = 3.0
    max_net_leverage: float = 1.0
    max_single_position: float = 0.5
    long_only: bool = False
    turnover_penalty: float = 0.0           # λ_turnover for L1 turnover penalty
    factor_neutral_exposures: NDArray | None = None  # (N, k) — zero out F'w
    risk_aversion: float = 1.0              # higher = more conservative


@dataclass(frozen=True)
class OptimizationResult:
    """Mean-variance optimization output."""

    weights: NDArray
    expected_return: float
    expected_vol: float
    expected_sharpe: float
    gross_leverage: float
    net_leverage: float
    turnover: float
    converged: bool
    iterations: int
    method: str
    objective_value: float


class MeanVarianceOptimizer:
    """Mean-variance optimizer (Markowitz with constraints).

    Solves the constrained QP via scipy.optimize.minimize (SLSQP).
    """

    def __init__(self, max_iters: int = 200, tol: float = 1e-8):
        self.max_iters = max_iters
        self.tol = tol

    def optimize(
        self,
        expected_returns: NDArray | list[float],
        cov_matrix: NDArray,
        constraints: OptimizationConstraints | None = None,
        previous_weights: NDArray | list[float] | None = None,
        initial_weights: NDArray | list[float] | None = None,
    ) -> OptimizationResult:
        """Maximize w'μ - (λ/2) w'Σw - turnover penalty, subject to caps.

        Args:
          expected_returns:  (N,) μ vector — alphas or residual returns
          cov_matrix:        (N, N) Σ
          constraints:       OptimizationConstraints (default: standard caps)
          previous_weights:  for turnover penalty; default = zeros
          initial_weights:   warm-start; default = small uniform weight

        Returns:
          `OptimizationResult` with weights + diagnostics.
        """
        mu = np.asarray(expected_returns, dtype=np.float64).ravel()
        Sigma = np.atleast_2d(np.asarray(cov_matrix, dtype=np.float64))
        N = mu.size
        if Sigma.shape != (N, N):
            raise ValueError(f"cov_matrix shape {Sigma.shape} != ({N}, {N})")
        if N == 0:
            return self._empty_result()

        c = constraints if constraints is not None else OptimizationConstraints()

        w_prev = (
            np.asarray(previous_weights, dtype=np.float64).ravel()
            if previous_weights is not None else np.zeros(N)
        )
        if w_prev.size != N:
            raise ValueError(f"previous_weights size {w_prev.size} != N={N}")

        w0 = (
            np.asarray(initial_weights, dtype=np.float64).ravel()
            if initial_weights is not None else np.full(N, 1.0 / N * 0.1)
        )

        # Bounds: per-asset cap, with long-only overriding to [0, cap]
        cap = c.max_single_position
        if c.long_only:
            bounds = [(0.0, cap)] * N
        else:
            bounds = [(-cap, cap)] * N

        # Constraint list for scipy
        scipy_constraints: list[dict] = []

        # Gross leverage ≤ max — written as max_gross - sum(|w|) >= 0.
        # SLSQP needs a smooth constraint; |w| isn't smooth, so we use sqrt(w^2 + eps).
        eps_smooth = 1e-10
        if np.isfinite(c.max_gross_leverage):
            scipy_constraints.append({
                "type": "ineq",
                "fun": lambda w: c.max_gross_leverage - np.sum(np.sqrt(w * w + eps_smooth)),
            })
        # Net leverage: sum(w) ≤ max AND sum(w) ≥ -max
        if np.isfinite(c.max_net_leverage):
            scipy_constraints.append({
                "type": "ineq",
                "fun": lambda w: c.max_net_leverage - np.sum(w),
            })
            scipy_constraints.append({
                "type": "ineq",
                "fun": lambda w: c.max_net_leverage + np.sum(w),
            })

        # Factor neutrality: F' w = 0  for each factor column
        if c.factor_neutral_exposures is not None:
            F = np.asarray(c.factor_neutral_exposures, dtype=np.float64)
            if F.ndim == 1:
                F = F.reshape(-1, 1)
            if F.shape[0] != N:
                raise ValueError(
                    f"factor_neutral_exposures rows {F.shape[0]} != N={N}"
                )
            for k in range(F.shape[1]):
                f_col = F[:, k].copy()  # capture by value, not reference
                scipy_constraints.append({
                    "type": "eq",
                    "fun": (lambda w, fc=f_col: float(fc @ w)),
                })

        # Objective: minimize -(w'μ) + (λ/2)(w'Σw) + turnover * ||w - w_prev||_1
        # Use smoothed L1 for differentiability: sqrt((w - w_prev)^2 + eps)
        def objective(w: NDArray) -> float:
            ret = float(w @ mu)
            var = float(w @ Sigma @ w)
            penalty = (
                c.turnover_penalty * float(np.sum(np.sqrt((w - w_prev) ** 2 + eps_smooth)))
                if c.turnover_penalty > 0 else 0.0
            )
            return -ret + 0.5 * c.risk_aversion * var + penalty

        def gradient(w: NDArray) -> NDArray:
            g = -mu + c.risk_aversion * (Sigma @ w)
            if c.turnover_penalty > 0:
                # d/dw of sqrt((w - w_prev)^2 + eps) = (w - w_prev) / sqrt(...)
                diff = w - w_prev
                g = g + c.turnover_penalty * (diff / np.sqrt(diff * diff + eps_smooth))
            return g

        try:
            res = minimize(
                objective,
                w0,
                jac=gradient,
                method="SLSQP",
                bounds=bounds,
                constraints=scipy_constraints,
                options={"maxiter": self.max_iters, "ftol": self.tol},
            )
            w = np.asarray(res.x, dtype=np.float64)
            converged = bool(res.success)
            iters = int(res.nit) if hasattr(res, "nit") else 0
            obj_value = float(res.fun)
        except Exception:
            w = np.zeros(N)
            converged = False
            iters = 0
            obj_value = float("nan")

        expected_return = float(w @ mu)
        expected_var = float(max(w @ Sigma @ w, 0.0))
        expected_vol = float(np.sqrt(expected_var))
        expected_sharpe = (
            expected_return / expected_vol if expected_vol > 0 else 0.0
        )
        turnover = float(np.sum(np.abs(w - w_prev)))

        return OptimizationResult(
            weights=w,
            expected_return=expected_return,
            expected_vol=expected_vol,
            expected_sharpe=expected_sharpe,
            gross_leverage=float(np.sum(np.abs(w))),
            net_leverage=float(w.sum()),
            turnover=turnover,
            converged=converged,
            iterations=iters,
            method="SLSQP",
            objective_value=obj_value,
        )

    def _empty_result(self) -> OptimizationResult:
        return OptimizationResult(
            weights=np.array([], dtype=np.float64),
            expected_return=0.0,
            expected_vol=0.0,
            expected_sharpe=0.0,
            gross_leverage=0.0,
            net_leverage=0.0,
            turnover=0.0,
            converged=False,
            iterations=0,
            method="empty",
            objective_value=0.0,
        )
