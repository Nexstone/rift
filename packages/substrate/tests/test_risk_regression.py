"""Tests for substrate.risk.regression — OLS+NW + Huber+NW wrappers.

Pins three invariants:
  1. Both estimators recover known coefficients from clean linear data.
  2. Newey-West produces LARGER SEs than naive OLS when residuals are serially
     correlated (the whole point of HAC).
  3. Huber resists outlier contamination that biases OLS.
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.risk.regression import (
    RegressionResult,
    huber_regression,
    ols_with_newey_west,
)
from rift_substrate.risk.regression import _newey_west_default_lag


# ─── Auto-lag rule ─────────────────────────────────────────────────────


class TestAutoLag:
    def test_newey_west_1994_rule(self):
        """floor(4 * (n/100)^(2/9))."""
        assert _newey_west_default_lag(100) == 4
        assert _newey_west_default_lag(50) == 3
        assert _newey_west_default_lag(1000) == 6
        assert _newey_west_default_lag(5000) == 9

    def test_zero_for_empty(self):
        assert _newey_west_default_lag(0) == 0


# ─── OLS+NW basic recovery ─────────────────────────────────────────────


class TestOLSRecovery:
    def test_recovers_known_coefficients(self):
        rng = np.random.default_rng(0)
        n = 500
        X = rng.normal(size=(n, 2))
        true_beta = np.array([1.5, -0.5])
        y = 0.1 + X @ true_beta + rng.normal(0, 0.2, n)
        r = ols_with_newey_west(y, X)
        # coef is [intercept, beta1, beta2]
        np.testing.assert_allclose(r.coef, np.array([0.1, 1.5, -0.5]), atol=0.05)
        assert r.n_obs == n
        assert r.n_params == 3  # intercept + 2 regressors
        assert "OLS+NW" in r.method
        assert r.r_squared > 0.9

    def test_intercept_omitted_when_disabled(self):
        rng = np.random.default_rng(1)
        n = 300
        X = rng.normal(size=(n, 1))
        y = X[:, 0] * 2.0 + rng.normal(0, 0.1, n)
        r = ols_with_newey_west(y, X, add_constant=False)
        assert r.n_params == 1
        np.testing.assert_allclose(r.coef, np.array([2.0]), atol=0.05)

    def test_handles_1d_X(self):
        """X passed as 1D should be reshaped to (n, 1)."""
        rng = np.random.default_rng(2)
        n = 200
        x = rng.normal(size=n)
        y = 1.0 + 2.0 * x + rng.normal(0, 0.05, n)
        r = ols_with_newey_west(y, x)
        assert r.coef.shape == (2,)
        np.testing.assert_allclose(r.coef, np.array([1.0, 2.0]), atol=0.05)


# ─── Newey-West > naive SEs on serial-correlated data ─────────────────


class TestNeweyWestCorrection:
    def test_NW_larger_than_naive_on_serially_correlated_mean(self):
        """Textbook NW-domination case: estimating the mean of an AR(1) series.

        For an intercept-only regression, naive OLS uses variance/n for the
        intercept SE — wrong when residuals are serially correlated. NW
        integrates the autocovariance and produces a meaningfully larger SE.

        (For a slope regression where X is iid mean-zero, the cross-product
        autocovariance can be small even when residuals are autocorrelated;
        the intercept-only test is the cleanest demonstration.)
        """
        rng = np.random.default_rng(3)
        n = 800
        rho = 0.7
        eps = np.zeros(n)
        eps[0] = rng.normal(0, 0.5)
        for t in range(1, n):
            eps[t] = rho * eps[t - 1] + rng.normal(0, 0.5)
        # y = AR(1) series, X = constant column → estimating the mean
        X_const = np.ones((n, 1))
        r_nw = ols_with_newey_west(eps, X_const, add_constant=False, nw_lag=None)
        r_naive = ols_with_newey_west(eps, X_const, add_constant=False, nw_lag=0)
        # NW SE should be substantially larger for rho=0.7 — at least 1.5x
        assert r_nw.se[0] > r_naive.se[0] * 1.5

    def test_explicit_lag_used(self):
        rng = np.random.default_rng(4)
        n = 200
        X = rng.normal(size=(n, 1))
        y = X[:, 0] + rng.normal(0, 0.3, n)
        r = ols_with_newey_west(y, X, nw_lag=8)
        assert r.nw_lag == 8
        assert "NW(8)" in r.method


# ─── Huber outlier robustness ─────────────────────────────────────────


class TestHuberRobustness:
    def test_huber_resists_outliers_that_bias_OLS(self):
        rng = np.random.default_rng(5)
        n = 400
        X = rng.normal(size=(n, 1))
        true_beta = np.array([0.0, 2.0])  # intercept 0, slope 2
        y = true_beta[0] + X[:, 0] * true_beta[1] + rng.normal(0, 0.2, n)
        # Add big outliers in y at every 20th index
        y_dirty = y.copy()
        y_dirty[::20] += 20.0

        r_ols = ols_with_newey_west(y_dirty, X)
        r_huber = huber_regression(y_dirty, X)

        # OLS intercept gets biased upward by the contamination
        # Huber stays near the truth
        ols_intercept_error = abs(r_ols.coef[0] - 0.0)
        huber_intercept_error = abs(r_huber.coef[0] - 0.0)
        assert huber_intercept_error < ols_intercept_error / 2

    def test_huber_recovers_when_no_outliers(self):
        """On clean data, Huber should agree with OLS up to small efficiency loss."""
        rng = np.random.default_rng(6)
        n = 400
        X = rng.normal(size=(n, 1))
        y = 1.0 + X[:, 0] * 2.0 + rng.normal(0, 0.2, n)
        r_ols = ols_with_newey_west(y, X)
        r_huber = huber_regression(y, X)
        # Coefficients should be very close
        np.testing.assert_allclose(r_huber.coef, r_ols.coef, atol=0.05)

    def test_huber_with_nw_lag_zero_uses_iid_SE(self):
        rng = np.random.default_rng(7)
        n = 300
        X = rng.normal(size=(n, 1))
        y = X[:, 0] + rng.normal(0, 0.3, n)
        r = huber_regression(y, X, nw_lag=0)
        assert r.method == "Huber"
        assert r.nw_lag == 0

    def test_huber_with_explicit_nw_lag(self):
        rng = np.random.default_rng(8)
        n = 300
        X = rng.normal(size=(n, 1))
        y = X[:, 0] + rng.normal(0, 0.3, n)
        r = huber_regression(y, X, nw_lag=5)
        assert "Huber+NW(5)" == r.method


# ─── Edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_too_few_observations_returns_degenerate(self):
        r = ols_with_newey_west([1.0, 2.0], [[1.0], [2.0]])
        # n=2, k=2 (intercept + 1) → n < k+2, should be degenerate
        assert "degenerate" in r.method
        assert np.all(np.isnan(r.coef))

    def test_nan_inputs_dropped(self):
        rng = np.random.default_rng(9)
        n = 300
        X = rng.normal(size=(n, 1))
        y = X[:, 0] * 2.0 + rng.normal(0, 0.2, n)
        # Inject NaNs in y
        y[::30] = np.nan
        r = ols_with_newey_west(y, X)
        # Should have dropped the NaN rows, recovered approx 2.0
        assert r.n_obs < n
        np.testing.assert_allclose(r.coef[1], 2.0, atol=0.1)

    def test_inf_inputs_dropped(self):
        rng = np.random.default_rng(10)
        n = 200
        X = rng.normal(size=(n, 1))
        y = X[:, 0] * 3.0 + rng.normal(0, 0.2, n)
        X[::40, 0] = np.inf
        r = ols_with_newey_west(y, X)
        assert r.n_obs < n
        np.testing.assert_allclose(r.coef[1], 3.0, atol=0.1)

    def test_shape_mismatch_raises(self):
        with pytest.raises(ValueError, match="shape mismatch"):
            ols_with_newey_west(np.zeros(10), np.zeros((5, 2)))

    def test_pvalues_in_unit_interval(self):
        rng = np.random.default_rng(11)
        X = rng.normal(size=(200, 2))
        y = rng.normal(size=200)
        r = ols_with_newey_west(y, X)
        assert np.all((r.pvalue >= 0) & (r.pvalue <= 1))
