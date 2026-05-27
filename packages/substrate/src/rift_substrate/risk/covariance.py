"""Covariance estimators — sample + Ledoit-Wolf shrinkage.

Two estimators with a common interface:

  SampleCovariance     — classical sample covariance. Unbiased but noisy
                         when N (assets) approaches T (observations); known
                         to produce ill-conditioned matrices that blow up
                         in optimization.

  LedoitWolfCovariance — Ledoit & Wolf (2003) shrinkage estimator. Shrinks
                         the sample covariance toward a structured target
                         (default: constant-correlation matrix) using an
                         optimal data-driven intensity. Produces stable,
                         well-conditioned covariances even at moderate
                         T/N. Industry-standard practice at AQR / Two Sigma /
                         BARRA-style equity-risk shops.

References:
  Ledoit, O. & Wolf, M. (2003). "Improved estimation of the covariance matrix
    of stock returns with an application to portfolio selection."
    Journal of Empirical Finance 10, 603-621.
  Ledoit, O. & Wolf, M. (2004). "Honey, I shrunk the sample covariance matrix."
    Journal of Portfolio Management 30, 110-119.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class CovarianceEstimate:
    """Result of fitting a covariance estimator on a returns panel.

    Attributes:
      cov:                (N, N) covariance matrix
      mean:               (N,) sample means
      asset_names:        list of N asset names
      n_observations:     T — number of return rows
      n_assets:           N
      method:             estimator label (e.g., "sample", "ledoit_wolf")
      shrinkage_lambda:   for shrinkage methods, the chosen intensity in [0,1].
                          NaN if not applicable.
      condition_number:   max(eigval) / min(eigval) — measures conditioning
    """

    cov: NDArray
    mean: NDArray
    asset_names: list[str] = field(default_factory=list)
    n_observations: int = 0
    n_assets: int = 0
    method: str = ""
    shrinkage_lambda: float = float("nan")
    condition_number: float = float("nan")

    def correlation(self) -> NDArray:
        """Pearson correlation matrix derived from the covariance."""
        sd = np.sqrt(np.diag(self.cov))
        sd[sd <= 0] = 1.0
        return self.cov / np.outer(sd, sd)

    def volatilities(self) -> NDArray:
        """Per-asset volatilities (sqrt of diagonal)."""
        return np.sqrt(np.maximum(np.diag(self.cov), 0.0))


# ─── Helpers ───────────────────────────────────────────────────────────


def _validate_returns(returns: NDArray | list, names: list[str] | None) -> tuple[NDArray, list[str]]:
    r = np.atleast_2d(np.asarray(returns, dtype=np.float64))
    if r.ndim != 2:
        raise ValueError(f"returns must be 2D (T, N); got shape {r.shape}")
    if r.shape[0] < 2:
        raise ValueError(f"need at least 2 observations; got T={r.shape[0]}")
    n_assets = r.shape[1]
    if names is None:
        names = [f"asset_{i}" for i in range(n_assets)]
    if len(names) != n_assets:
        raise ValueError(f"names ({len(names)}) != n_assets ({n_assets})")
    return r, list(names)


def _condition_number(M: NDArray) -> float:
    # Eigenvalue computation on ill-conditioned matrices can produce noisy
    # matmul warnings — they're cosmetic, the result is well-defined.
    with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
        try:
            eigvals = np.linalg.eigvalsh(M)
        except np.linalg.LinAlgError:
            return float("inf")
    eigvals = eigvals[eigvals > 1e-14]
    if eigvals.size == 0:
        return float("inf")
    return float(eigvals.max() / eigvals.min())


# ─── Estimators ────────────────────────────────────────────────────────


class CovarianceEstimator(ABC):
    """Abstract base — fit on a (T, N) returns panel, return a CovarianceEstimate."""

    name: str = "covariance"

    @abstractmethod
    def fit(
        self,
        returns: NDArray | list,
        asset_names: list[str] | None = None,
    ) -> CovarianceEstimate:
        raise NotImplementedError


class SampleCovariance(CovarianceEstimator):
    """Classical sample covariance with the ddof=1 (unbiased) denominator.

    Use only when T >> N (e.g., 20+ observations per asset). With T ~ N
    or T < N, prefer `LedoitWolfCovariance`.
    """

    name: str = "sample"

    def fit(
        self,
        returns: NDArray | list,
        asset_names: list[str] | None = None,
    ) -> CovarianceEstimate:
        r, names = _validate_returns(returns, asset_names)
        T, N = r.shape
        mean = r.mean(axis=0)
        cov = np.cov(r, rowvar=False, ddof=1)
        if N == 1:
            # np.cov returns scalar for N=1; promote to 1x1
            cov = np.atleast_2d(cov)
        return CovarianceEstimate(
            cov=cov,
            mean=mean,
            asset_names=names,
            n_observations=int(T),
            n_assets=int(N),
            method=self.name,
            shrinkage_lambda=float("nan"),
            condition_number=_condition_number(cov),
        )


class LedoitWolfCovariance(CovarianceEstimator):
    """Ledoit-Wolf shrinkage covariance.

    Computes:
      Σ̂ = δ * F + (1 - δ) * S

    where:
      S      — sample covariance matrix
      F      — shrinkage target. Default: constant-correlation matrix
               (the average pairwise correlation, applied across all
               off-diagonal entries; diagonal preserved from S).
      δ      — optimal shrinkage intensity in [0, 1], chosen to minimize
               expected Frobenius distance from the true Σ. Computed
               via the Ledoit-Wolf (2003) formula.

    For the constant-correlation target F:
      F_{ii}  = S_{ii}
      F_{ij}  = ρ_avg * sqrt(S_{ii} * S_{jj})   (i ≠ j)
      where ρ_avg = average of the off-diagonal sample correlations.
    """

    name: str = "ledoit_wolf"

    def __init__(self, target: str = "constant_correlation"):
        if target not in ("constant_correlation", "identity"):
            raise ValueError(
                f"target must be 'constant_correlation' or 'identity'; got {target!r}"
            )
        self.target = target

    def fit(
        self,
        returns: NDArray | list,
        asset_names: list[str] | None = None,
    ) -> CovarianceEstimate:
        r, names = _validate_returns(returns, asset_names)
        T, N = r.shape
        mean = r.mean(axis=0)
        # Centered returns for the LW formula
        x = r - mean

        # Sample covariance with biased denominator T (LW formula assumes this).
        # Matmul on poorly-scaled inputs can emit cosmetic numpy warnings; the
        # math here is well-defined so we suppress.
        with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
            S = (x.T @ x) / T
        if N == 1:
            return CovarianceEstimate(
                cov=np.atleast_2d(S),
                mean=mean,
                asset_names=names,
                n_observations=int(T),
                n_assets=int(N),
                method=self.name,
                shrinkage_lambda=0.0,
                condition_number=1.0,
            )

        # Build target F
        if self.target == "constant_correlation":
            F = self._constant_correlation_target(S)
        else:  # identity
            avg_var = float(np.mean(np.diag(S)))
            F = avg_var * np.eye(N)

        # Optimal shrinkage intensity (Ledoit-Wolf 2003 closed form)
        delta = self._ledoit_wolf_intensity(x, S, F, T, N)
        delta = float(np.clip(delta, 0.0, 1.0))

        # Shrunk covariance — convert biased S back to unbiased for the
        # final estimate so downstream code sees the conventional cov.
        # (delta operates on the biased form; we rescale after blending.)
        S_unbiased = S * (T / (T - 1)) if T > 1 else S
        cov = delta * F + (1.0 - delta) * S_unbiased

        return CovarianceEstimate(
            cov=cov,
            mean=mean,
            asset_names=names,
            n_observations=int(T),
            n_assets=int(N),
            method=self.name,
            shrinkage_lambda=delta,
            condition_number=_condition_number(cov),
        )

    @staticmethod
    def _constant_correlation_target(S: NDArray) -> NDArray:
        """F: diagonal = S; off-diagonal = ρ_avg * sqrt(S_ii * S_jj)."""
        N = S.shape[0]
        sd = np.sqrt(np.maximum(np.diag(S), 0.0))
        # Sample correlation matrix (handle zero-variance assets safely)
        sd_safe = np.where(sd > 0, sd, 1.0)
        R = S / np.outer(sd_safe, sd_safe)
        np.fill_diagonal(R, 1.0)
        # Average off-diagonal correlation
        mask = ~np.eye(N, dtype=bool)
        rho_avg = float(R[mask].mean()) if mask.any() else 0.0
        # Reconstruct F
        F = rho_avg * np.outer(sd, sd)
        np.fill_diagonal(F, np.diag(S))
        return F

    @staticmethod
    def _ledoit_wolf_intensity(
        x: NDArray,  # (T, N) centered returns
        S: NDArray,  # (N, N) biased sample cov
        F: NDArray,  # (N, N) target
        T: int,
        N: int,
    ) -> float:
        """Ledoit-Wolf 2003 optimal shrinkage intensity δ*.

        δ* ≈ κ / T, where:
          π̂  = sum_{i,j} Var(s_{ij})       (estimation noise)
          γ̂  = ||F - S||_F^2                (target misspecification)
          ρ̂  = sum_{i,j} ACov(f_{ij}, s_{ij})  (asymptotic covariance)
          κ̂  = (π̂ - ρ̂) / γ̂
        """
        # Squared centered returns — needed for variance-of-cov estimates
        x2 = x ** 2

        # π̂: estimation noise of the sample covariance entries
        with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
            pi_mat = (x2.T @ x2) / T - S ** 2
        pi_hat = float(pi_mat.sum())

        # γ̂: distance between target and sample
        diff = F - S
        gamma_hat = float((diff ** 2).sum())
        if gamma_hat <= 1e-12:
            # Target ≈ sample; full sample (no shrinkage helps)
            return 0.0

        # ρ̂: for constant-correlation target, the diagonal contribution +
        # off-diagonal contribution per LW 2003 derivation.
        # Implementation follows the standard formula from
        # Ledoit & Wolf (2003) Appendix A.
        # Diagonal: pi_ii's (variance of diagonal of S)
        rho_diag = float(np.diag(pi_mat).sum())

        # Off-diagonal: ρ̂_off = sum_{i ≠ j} (ρ_avg / 2) * (
        #   sqrt(s_jj/s_ii) * θ_iiij + sqrt(s_ii/s_jj) * θ_jjij
        # )
        # where θ_iiij = (1/T) sum_t x_{t,i}^2 * x_{t,i} * x_{t,j} - s_ii * s_ij
        # and similarly for θ_jjij.
        sd = np.sqrt(np.maximum(np.diag(S), 0.0))
        sd_safe = np.where(sd > 0, sd, 1.0)
        R = S / np.outer(sd_safe, sd_safe)
        np.fill_diagonal(R, 1.0)
        mask = ~np.eye(N, dtype=bool)
        rho_avg = float(R[mask].mean()) if mask.any() else 0.0

        # Build θ matrix:  θ_{kl,ij} = (1/T) sum_t (x_{t,k} x_{t,l}) (x_{t,i} x_{t,j})  - s_{kl} s_{ij}
        # We need θ_{ii,ij}; i.e., for each pair (i, j ≠ i) we want θ for k=l=i.
        # θ_{ii,ij} = (1/T) sum_t x_i^2 * x_i * x_j - s_{ii} * s_{ij}
        #           = (1/T) sum_t x_i^3 * x_j  - s_{ii} * s_{ij}
        rho_off = 0.0
        if N >= 2 and rho_avg != 0:
            for i in range(N):
                for j in range(N):
                    if i == j:
                        continue
                    # θ_iiij
                    theta_ii_ij = float((x[:, i] ** 3 * x[:, j]).mean() - S[i, i] * S[i, j])
                    # θ_jjij
                    theta_jj_ij = float((x[:, j] ** 3 * x[:, i]).mean() - S[j, j] * S[i, j])
                    if sd[i] > 0 and sd[j] > 0:
                        rho_off += (rho_avg / 2) * (
                            (sd[j] / sd[i]) * theta_ii_ij + (sd[i] / sd[j]) * theta_jj_ij
                        )

        rho_hat = rho_diag + rho_off

        kappa = (pi_hat - rho_hat) / gamma_hat
        delta = kappa / T
        return float(delta)
