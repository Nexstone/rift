"""Unit tests for performance metrics."""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.stats.metrics import (
    CRYPTO_DAILY,
    CRYPTO_HOURLY,
    MetricBundle,
    Stats,
    annual_return,
    annual_vol,
    autocorrelation,
    calmar_ratio,
    kurtosis,
    max_drawdown,
    sharpe_ratio,
    skewness,
    sortino_ratio,
)


class TestSharpeRatio:
    def test_zero_mean_returns_zero_sharpe(self):
        # Returns with mean 0 → Sharpe 0
        r = np.array([0.01, -0.01, 0.01, -0.01, 0.01, -0.01])
        sh = sharpe_ratio(r, periods_per_year=365)
        assert abs(sh) < 0.01

    def test_constant_positive_returns_high_sharpe(self):
        # Constant positive returns → infinite Sharpe (sd=0).
        # Use slightly noisy positive returns: should be very high.
        rng = np.random.default_rng(42)
        r = 0.001 + rng.normal(0, 0.0001, size=100)
        sh = sharpe_ratio(r, periods_per_year=365)
        assert sh > 5  # very high Sharpe

    def test_negative_returns_negative_sharpe(self):
        rng = np.random.default_rng(42)
        r = -0.001 + rng.normal(0, 0.005, size=200)
        sh = sharpe_ratio(r, periods_per_year=365)
        assert sh < 0

    def test_zero_variance_returns_nan(self):
        r = np.array([0.001] * 100)
        sh = sharpe_ratio(r, periods_per_year=365)
        assert np.isnan(sh)

    def test_too_few_observations_returns_nan(self):
        sh = sharpe_ratio(np.array([0.01]), periods_per_year=365)
        assert np.isnan(sh)


class TestSortinoRatio:
    def test_only_positive_returns_infinite(self):
        r = np.array([0.01, 0.02, 0.01, 0.03, 0.01])
        so = sortino_ratio(r, periods_per_year=365)
        assert so == float("inf")

    def test_mixed_returns_finite(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0.001, 0.01, size=200)
        so = sortino_ratio(r, periods_per_year=365)
        assert np.isfinite(so)


class TestMaxDrawdown:
    def test_monotonically_increasing_no_drawdown(self):
        r = np.array([0.01] * 100)
        mdd = max_drawdown(r)
        assert mdd == 0.0

    def test_50pct_drawdown(self):
        # +100% then -75% = 2.0 → 0.5 = -75% drawdown
        r = np.array([1.0, -0.75])
        mdd = max_drawdown(r)
        assert abs(mdd - (-0.75)) < 1e-6

    def test_drawdown_is_negative(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0, 0.02, size=500)
        mdd = max_drawdown(r)
        assert mdd <= 0


class TestCalmar:
    def test_positive_return_negative_dd_positive_calmar(self):
        rng = np.random.default_rng(42)
        r = 0.001 + rng.normal(0, 0.005, size=500)
        cal = calmar_ratio(r, periods_per_year=365)
        assert cal > 0


class TestDistributionMoments:
    def test_skew_of_normal_near_zero(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0, 1, size=5000)
        sk = skewness(r)
        assert abs(sk) < 0.1

    def test_kurtosis_of_normal_near_3(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0, 1, size=5000)
        kt = kurtosis(r)
        assert abs(kt - 3.0) < 0.5

    def test_skew_of_right_skewed_positive(self):
        rng = np.random.default_rng(42)
        # lognormal is right-skewed
        r = rng.lognormal(0, 1, size=2000)
        sk = skewness(r)
        assert sk > 1

    def test_autocorrelation_iid_near_zero(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0, 1, size=2000)
        ac = autocorrelation(r, lag=1)
        assert abs(ac) < 0.1

    def test_autocorrelation_ar1_recovers_rho(self):
        rng = np.random.default_rng(42)
        rho = 0.7
        n = 2000
        x = np.zeros(n)
        x[0] = rng.normal()
        for i in range(1, n):
            x[i] = rho * x[i - 1] + rng.normal()
        ac = autocorrelation(x, lag=1)
        assert abs(ac - rho) < 0.1


class TestMetricBundle:
    def test_from_returns_populates_all_fields(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0.001, 0.01, size=500)
        mb = Stats.from_returns(r, periods_per_year=CRYPTO_DAILY,
                                  n_bootstrap=100, seed=1)
        assert isinstance(mb, MetricBundle)
        assert mb.n_observations == 500
        assert np.isfinite(mb.sharpe)
        assert np.isfinite(mb.sortino)
        assert np.isfinite(mb.annual_return)
        assert np.isfinite(mb.max_drawdown)
        # CI should bracket the point estimate
        lo, hi = mb.sharpe_ci_95
        assert lo <= mb.sharpe <= hi or abs(lo - mb.sharpe) < 0.5

    def test_summary_returns_string(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0.001, 0.01, size=200)
        mb = Stats.from_returns(r, periods_per_year=CRYPTO_DAILY,
                                  n_bootstrap=50, seed=1)
        s = mb.summary()
        assert "Annual return" in s
        assert "Sharpe" in s
        assert "Max DD" in s

    def test_reproducible_with_seed(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0.001, 0.01, size=200)
        a = Stats.from_returns(r, periods_per_year=CRYPTO_DAILY,
                                 n_bootstrap=50, seed=7)
        b = Stats.from_returns(r, periods_per_year=CRYPTO_DAILY,
                                 n_bootstrap=50, seed=7)
        assert a.sharpe_ci_95 == b.sharpe_ci_95

    def test_rejects_tiny_input(self):
        with pytest.raises(ValueError, match="at least 2"):
            Stats.from_returns([0.01], periods_per_year=CRYPTO_DAILY)

    def test_handles_hourly_periods(self):
        rng = np.random.default_rng(42)
        r = rng.normal(0.0001, 0.001, size=500)
        mb = Stats.from_returns(r, periods_per_year=CRYPTO_HOURLY,
                                  n_bootstrap=50, seed=1)
        # Annual return should be reasonable (annualized properly)
        assert -1 < mb.annual_return < 10  # generous bounds
