"""Regression helpers for factor analysis.

Wraps statsmodels for:
  - OLS with Newey-West HAC standard errors (corrects for serial correlation)
  - Huber M-estimator with optional Newey-West sandwich SEs (robust to outliers)
  - Auto-picked NW lag via the Newey-West 1994 rule

Crypto returns have fat tails AND serial correlation. Naive OLS gives
incorrect t-statistics on both axes; Huber-IID corrects for fat tails
but not serial correlation; OLS+HAC corrects for serial correlation but
not fat tails. Use the full Huber+NW combination when both are concerns.

References:
  Newey, W. K. & West, K. D. (1987). "A simple, positive semi-definite,
    heteroskedasticity and autocorrelation consistent covariance matrix."
    Econometrica 55, 703-708.
  Newey, W. K. & West, K. D. (1994). "Automatic lag selection in covariance
    matrix estimation." Review of Economic Studies 61, 631-653.
  Huber, P. J. (1964). "Robust estimation of a location parameter."
    Annals of Mathematical Statistics 35, 73-101.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import statsmodels.api as sm
from numpy.typing import NDArray
from scipy.stats import norm
from statsmodels.robust.robust_linear_model import RLM


@dataclass(frozen=True)
class RegressionResult:
    """Parameter estimates + corrected SEs + diagnostics from a regression.

    Coefficient at index 0 is the intercept (alpha) when `add_constant=True`
    was passed; subsequent indices align with the columns of X.
    """

    coef: NDArray           # parameter estimates (length k or k+1 if intercept)
    se: NDArray             # standard errors (HAC-corrected when applicable)
    tstat: NDArray          # coef / se
    pvalue: NDArray         # two-sided p-values from normal approx
    r_squared: float        # 1 - SSR/SST. NaN if not well-defined.
    residuals: NDArray      # y - X @ coef
    n_obs: int              # number of observations after dropping NaN rows
    n_params: int           # number of fitted parameters
    method: str             # e.g., "OLS+NW(4)" or "Huber+NW(6)" or "OLS"
    nw_lag: int             # truncation lag used (0 = no NW correction)


# ─── Helpers ───────────────────────────────────────────────────────────


def _drop_invalid_rows(y: NDArray, X: NDArray) -> tuple[NDArray, NDArray]:
    """Drop rows where any of y or X is NaN/inf."""
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    return y[mask], X[mask]


def _newey_west_default_lag(n: int) -> int:
    """Newey-West (1994) automatic lag selection: floor(4 * (n/100)^(2/9))."""
    if n < 1:
        return 0
    return int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))


def _newey_west_sandwich(
    X: NDArray, residuals: NDArray, hat_inv: NDArray, lag: int
) -> NDArray:
    """Compute Newey-West HAC variance matrix given a residual vector.

    Sandwich estimator:  V = (X'X)^-1 · S · (X'X)^-1
      S = sum_{j=-L}^{L} w_j · sum_t (x_t * x_{t-|j|}' · u_t · u_{t-|j|})
      w_j = 1 - |j| / (L+1)   (Bartlett kernel)

    For OLS, hat_inv = (X'X)^-1.
    For Huber with weights w, hat_inv = (X' W X)^-1 and residuals are the
    Huber residuals (already psi-transformed when used appropriately).
    """
    n, k = X.shape
    # Build the "meat" matrix S
    # j=0 contribution
    XU = X * residuals.reshape(-1, 1)
    S = XU.T @ XU
    # j>0 contributions (Bartlett kernel)
    for j in range(1, lag + 1):
        w = 1.0 - j / (lag + 1.0)
        gamma = XU[j:].T @ XU[:-j]
        S = S + w * (gamma + gamma.T)
    return hat_inv @ S @ hat_inv


# ─── Public API ────────────────────────────────────────────────────────


def ols_with_newey_west(
    y: NDArray | list,
    X: NDArray | list,
    *,
    add_constant: bool = True,
    nw_lag: int | None = None,
) -> RegressionResult:
    """OLS regression with Newey-West HAC standard errors.

    Args:
      y:            dependent variable, shape (n,)
      X:            independent variables, shape (n, k) — or (n,) for single regressor
      add_constant: prepend a column of ones for intercept (default True)
      nw_lag:       NW truncation lag. None = auto (Newey-West 1994 rule).
                    Pass 0 explicitly to skip the correction and get plain OLS SEs.

    Returns:
      RegressionResult with HAC-corrected SEs and t-stats.

    Edge cases:
      - Insufficient observations (< n_params + 2) → NaN-filled result
      - Rank-deficient X → NaN-filled result with method='degenerate'
      - All-NaN inputs → empty result
    """
    y_arr = np.asarray(y, dtype=np.float64).ravel()
    X_arr = np.atleast_2d(np.asarray(X, dtype=np.float64))
    if X_arr.shape[0] != y_arr.size:
        # X might be (k, n) instead of (n, k)
        if X_arr.shape[1] == y_arr.size:
            X_arr = X_arr.T
        else:
            raise ValueError(f"shape mismatch: y={y_arr.shape}, X={X_arr.shape}")

    y_arr, X_arr = _drop_invalid_rows(y_arr, X_arr)
    if add_constant:
        X_arr = sm.add_constant(X_arr, has_constant="add")
    n, k = X_arr.shape
    lag = _newey_west_default_lag(n) if nw_lag is None else int(nw_lag)

    if n < k + 2:
        return _empty_result(k, "OLS+NW", lag)

    try:
        if lag > 0:
            fit = sm.OLS(y_arr, X_arr).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
            method = f"OLS+NW({lag})"
        else:
            fit = sm.OLS(y_arr, X_arr).fit()
            method = "OLS"
    except Exception:
        return _empty_result(k, "OLS+NW", lag)

    coef = np.asarray(fit.params, dtype=np.float64)
    se = np.asarray(fit.bse, dtype=np.float64)
    # p-values from normal approximation (large-n) — statsmodels uses t but
    # for HAC the asymptotic distribution is normal anyway
    tstat = np.divide(coef, se, out=np.full_like(coef, np.nan), where=se > 0)
    pvalue = 2.0 * (1.0 - norm.cdf(np.abs(tstat)))

    return RegressionResult(
        coef=coef,
        se=se,
        tstat=tstat,
        pvalue=pvalue,
        r_squared=float(fit.rsquared) if hasattr(fit, "rsquared") else float("nan"),
        residuals=np.asarray(fit.resid, dtype=np.float64),
        n_obs=int(n),
        n_params=int(k),
        method=method,
        nw_lag=int(lag),
    )


def huber_regression(
    y: NDArray | list,
    X: NDArray | list,
    *,
    add_constant: bool = True,
    nw_lag: int | None = None,
) -> RegressionResult:
    """Huber M-estimator regression. Robust to residual outliers.

    Args:
      y, X, add_constant, nw_lag: as in `ols_with_newey_west`.

      nw_lag controls Newey-West correction on the Huber residuals:
        None → auto (Newey-West 1994 rule)
        0    → use statsmodels' built-in Huber SE (no serial-correlation correction)
        >0   → compute the Huber+NW sandwich SE manually

    Returns:
      RegressionResult. Method label distinguishes "Huber+NW(L)" from "Huber".

    Why this exists: crypto returns are fat-tailed AND serially correlated.
    Plain OLS gets fooled by outliers; OLS+NW gets fooled too; Huber alone
    has bad SEs when residuals are serially correlated. Huber+NW handles both.
    """
    y_arr = np.asarray(y, dtype=np.float64).ravel()
    X_arr = np.atleast_2d(np.asarray(X, dtype=np.float64))
    if X_arr.shape[0] != y_arr.size:
        if X_arr.shape[1] == y_arr.size:
            X_arr = X_arr.T
        else:
            raise ValueError(f"shape mismatch: y={y_arr.shape}, X={X_arr.shape}")

    y_arr, X_arr = _drop_invalid_rows(y_arr, X_arr)
    if add_constant:
        X_arr = sm.add_constant(X_arr, has_constant="add")
    n, k = X_arr.shape
    lag = _newey_west_default_lag(n) if nw_lag is None else int(nw_lag)

    if n < k + 2:
        return _empty_result(k, "Huber+NW", lag)

    try:
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            rlm = RLM(y_arr, X_arr, M=sm.robust.norms.HuberT())
            fit = rlm.fit()
    except Exception:
        return _empty_result(k, "Huber+NW", lag)

    coef = np.asarray(fit.params, dtype=np.float64)
    residuals = np.asarray(fit.resid, dtype=np.float64)

    if lag > 0:
        # Build the Huber+NW sandwich SE.
        # Hat_inv = (X' W X)^-1 where W = diag(huber_weights)
        weights = np.asarray(fit.weights, dtype=np.float64)
        XtWX = X_arr.T @ (X_arr * weights.reshape(-1, 1))
        try:
            hat_inv = np.linalg.inv(XtWX)
        except np.linalg.LinAlgError:
            return _empty_result(k, "Huber+NW", lag)
        # For Huber, the influence function is psi(u) = u when |u| ≤ c, c*sign(u) otherwise.
        # The "score" residuals used in the sandwich are weight * residual.
        psi_resid = weights * residuals
        cov = _newey_west_sandwich(X_arr, psi_resid, hat_inv, lag)
        se = np.sqrt(np.maximum(np.diag(cov), 0.0))
        method = f"Huber+NW({lag})"
    else:
        se = np.asarray(fit.bse, dtype=np.float64)
        method = "Huber"

    tstat = np.divide(coef, se, out=np.full_like(coef, np.nan), where=se > 0)
    pvalue = 2.0 * (1.0 - norm.cdf(np.abs(tstat)))

    # Pseudo-R²: 1 - SSR/SST using ORIGINAL y (not the down-weighted version).
    # Wrap matmul + arithmetic to suppress warnings when Huber's iterative
    # reweighting on outlier-laden data produces numerically extreme coefs
    # (the result is still well-defined; the warnings are cosmetic).
    with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
        fitted = X_arr @ coef
        sse = float(np.sum((y_arr - fitted) ** 2))
        sst = float(np.sum((y_arr - y_arr.mean()) ** 2))
        r_squared = 1.0 - sse / sst if sst > 0 else float("nan")

    return RegressionResult(
        coef=coef,
        se=se,
        tstat=tstat,
        pvalue=pvalue,
        r_squared=r_squared,
        residuals=residuals,
        n_obs=int(n),
        n_params=int(k),
        method=method,
        nw_lag=int(lag),
    )


def _empty_result(k: int, method_root: str, lag: int) -> RegressionResult:
    nan_vec = np.full(k, np.nan)
    return RegressionResult(
        coef=nan_vec,
        se=nan_vec.copy(),
        tstat=nan_vec.copy(),
        pvalue=nan_vec.copy(),
        r_squared=float("nan"),
        residuals=np.array([], dtype=np.float64),
        n_obs=0,
        n_params=k,
        method=f"{method_root}(degenerate)",
        nw_lag=int(lag),
    )
