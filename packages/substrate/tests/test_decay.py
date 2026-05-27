"""Tests for substrate.decay — alpha decay analysis.

Pins:
  1. make_forward_returns returns expected shape + NaN tail pattern
  2. IC curve matches numpy corrcoef on a known signal/return pair
  3. Spearman vs Pearson agree on linear monotone data
  4. Half-life recovers the true τ on synthetic exponential decay
  5. Constant IC → half_life = +inf (no decay)
  6. Mismatched shapes raise ValueError
  7. Bootstrap CIs are populated when n_bootstrap > 0
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.decay import (
    AlphaDecayCurve,
    HalfLifeFit,
    compute_ic_curve,
    estimate_half_life,
    make_forward_returns,
)


# ─── make_forward_returns ────────────────────────────────────────────


class TestMakeForwardReturns:
    def test_shape_and_basic_values(self):
        prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        fr = make_forward_returns(prices, [1, 2])
        assert fr.shape == (5, 2)
        # fr[0, 0] = 101/100 - 1 = 0.01
        assert fr[0, 0] == pytest.approx(0.01)
        # fr[0, 1] = 102/100 - 1 = 0.02
        assert fr[0, 1] == pytest.approx(0.02)
        # fr[3, 1] would be price[5]/price[3] but no price[5] → NaN
        assert np.isnan(fr[3, 1])
        assert np.isnan(fr[4, 1])

    def test_empty_horizons(self):
        prices = np.array([100.0, 101.0, 102.0])
        fr = make_forward_returns(prices, [])
        assert fr.shape == (3, 0)

    def test_zero_horizon_raises(self):
        with pytest.raises(ValueError, match="horizons must be"):
            make_forward_returns([100, 101], [0, 1])

    def test_negative_horizon_raises(self):
        with pytest.raises(ValueError, match="horizons must be"):
            make_forward_returns([100, 101, 102], [-1])

    def test_horizon_exceeds_length(self):
        """If horizon > T, the whole column is NaN."""
        prices = np.array([100.0, 101.0, 102.0])
        fr = make_forward_returns(prices, [5])
        assert fr.shape == (3, 1)
        assert np.all(np.isnan(fr))


# ─── compute_ic_curve ────────────────────────────────────────────────


class TestComputeICCurve:
    def test_perfect_correlation_at_one_horizon(self):
        rng = np.random.default_rng(0)
        signal = rng.standard_normal(500)
        fr = signal.reshape(-1, 1)  # one horizon, return = signal exactly
        curve = compute_ic_curve(signal, fr, [1], method="pearson")
        assert curve.ics[0] == pytest.approx(1.0)

    def test_zero_correlation_at_random_horizon(self):
        rng = np.random.default_rng(0)
        signal = rng.standard_normal(2000)
        noise = rng.standard_normal((2000, 1))  # independent
        curve = compute_ic_curve(signal, noise, [1], method="pearson")
        # Should be close to 0 (large sample → low sampling noise)
        assert abs(curve.ics[0]) < 0.1

    def test_horizons_aligned_with_columns(self):
        rng = np.random.default_rng(0)
        signal = rng.standard_normal(500)
        # h=1 column: return = signal exactly
        # h=2 column: pure noise
        fr = np.column_stack([signal, rng.standard_normal(500)])
        curve = compute_ic_curve(signal, fr, [1, 2], method="pearson")
        assert curve.ics[0] == pytest.approx(1.0)
        assert abs(curve.ics[1]) < 0.15

    def test_spearman_matches_pearson_on_monotone_data(self):
        """For perfectly linear data, Pearson == Spearman = 1.0."""
        signal = np.arange(100, dtype=np.float64)
        fr = signal.reshape(-1, 1) * 2.0 + 10  # linear transform
        curve_p = compute_ic_curve(signal, fr, [1], method="pearson")
        curve_s = compute_ic_curve(signal, fr, [1], method="spearman")
        assert curve_p.ics[0] == pytest.approx(1.0)
        assert curve_s.ics[0] == pytest.approx(1.0)

    def test_constant_signal_gives_nan_ic(self):
        signal = np.ones(100)
        fr = np.random.default_rng(0).standard_normal((100, 1))
        curve = compute_ic_curve(signal, fr, [1])
        assert np.isnan(curve.ics[0])

    def test_mismatched_shapes_raise(self):
        signal = np.arange(100, dtype=np.float64)
        fr = np.random.default_rng(0).standard_normal((50, 2))
        with pytest.raises(ValueError, match="signal length"):
            compute_ic_curve(signal, fr, [1, 2])

    def test_wrong_horizons_count_raises(self):
        signal = np.arange(100, dtype=np.float64)
        fr = np.random.default_rng(0).standard_normal((100, 2))
        with pytest.raises(ValueError, match="horizons"):
            compute_ic_curve(signal, fr, [1, 2, 3])  # 3 horizons but 2 columns

    def test_invalid_method_raises(self):
        signal = np.arange(50, dtype=np.float64)
        fr = signal.reshape(-1, 1)
        with pytest.raises(ValueError, match="method"):
            compute_ic_curve(signal, fr, [1], method="kendall")

    def test_bootstrap_populates_cis(self):
        rng = np.random.default_rng(0)
        signal = rng.standard_normal(500)
        fr = (signal + 0.5 * rng.standard_normal(500)).reshape(-1, 1)
        curve = compute_ic_curve(
            signal, fr, [1], method="pearson",
            n_bootstrap=100, seed=42,
        )
        assert np.isfinite(curve.ic_ci_lower[0])
        assert np.isfinite(curve.ic_ci_upper[0])
        assert curve.ic_ci_lower[0] <= curve.ics[0] <= curve.ic_ci_upper[0]
        assert curve.n_bootstrap == 100

    def test_no_bootstrap_leaves_cis_nan(self):
        signal = np.random.default_rng(0).standard_normal(100)
        fr = signal.reshape(-1, 1)
        curve = compute_ic_curve(signal, fr, [1], n_bootstrap=0)
        assert np.all(np.isnan(curve.ic_ci_lower))
        assert np.all(np.isnan(curve.ic_ci_upper))

    def test_summary_renders(self):
        signal = np.arange(50, dtype=np.float64)
        fr = signal.reshape(-1, 1)
        curve = compute_ic_curve(signal, fr, [1])
        s = curve.summary()
        assert "AlphaDecayCurve" in s
        assert "IC" in s


# ─── estimate_half_life ──────────────────────────────────────────────


class TestEstimateHalfLife:
    def test_recovers_true_half_life(self):
        """Synthetic exponential decay with τ=10 should recover half-life ≈ 6.93."""
        rng = np.random.default_rng(42)
        T = 10_000
        H = 30
        signal = rng.standard_normal(T)
        true_tau = 10.0
        true_hl = true_tau * np.log(2)
        noise_std = 10.0

        fr = np.full((T, H), np.nan)
        for h in range(1, H + 1):
            weight = np.exp(-h / true_tau)
            fr[:T - h, h - 1] = (
                weight * signal[:T - h] + noise_std * rng.standard_normal(T - h)
            )

        curve = compute_ic_curve(
            signal, fr, np.arange(1, H + 1), method="pearson",
        )
        fit = estimate_half_life(curve)
        # Within 20% (noisy regime — high horizons have weak signal)
        assert abs(fit.half_life - true_hl) / true_hl < 0.20

    def test_constant_ic_gives_inf_half_life(self):
        """If IC is constant, there's no decay → half_life = +inf."""
        curve = AlphaDecayCurve(
            horizons=np.array([1, 2, 5, 10]),
            ics=np.array([0.05, 0.05, 0.05, 0.05]),
            ic_ci_lower=np.full(4, np.nan),
            ic_ci_upper=np.full(4, np.nan),
            method="pearson",
            n_observations=1000,
        )
        fit = estimate_half_life(curve)
        assert np.isinf(fit.half_life)
        assert fit.ic_initial == pytest.approx(0.05)

    def test_growing_ic_gives_inf_half_life(self):
        """If IC grows with horizon, slope is positive → no decay."""
        curve = AlphaDecayCurve(
            horizons=np.array([1, 2, 5, 10]),
            ics=np.array([0.02, 0.04, 0.06, 0.10]),
            ic_ci_lower=np.full(4, np.nan),
            ic_ci_upper=np.full(4, np.nan),
            method="pearson",
            n_observations=1000,
        )
        fit = estimate_half_life(curve)
        assert np.isinf(fit.half_life)

    def test_too_few_points_returns_nan(self):
        curve = AlphaDecayCurve(
            horizons=np.array([1]),
            ics=np.array([0.05]),
            ic_ci_lower=np.array([np.nan]),
            ic_ci_upper=np.array([np.nan]),
            method="pearson",
            n_observations=100,
        )
        fit = estimate_half_life(curve)
        assert np.isnan(fit.half_life)

    def test_all_nan_ics_returns_nan(self):
        curve = AlphaDecayCurve(
            horizons=np.array([1, 2, 5]),
            ics=np.array([np.nan, np.nan, np.nan]),
            ic_ci_lower=np.full(3, np.nan),
            ic_ci_upper=np.full(3, np.nan),
            method="pearson",
            n_observations=100,
        )
        fit = estimate_half_life(curve)
        assert np.isnan(fit.half_life)

    def test_handles_negative_ics_via_abs(self):
        """Sign-flipped signal: ICs are negative but |IC| decays → finite half-life."""
        curve = AlphaDecayCurve(
            horizons=np.array([1, 2, 5, 10]),
            ics=np.array([-0.10, -0.07, -0.04, -0.02]),
            ic_ci_lower=np.full(4, np.nan),
            ic_ci_upper=np.full(4, np.nan),
            method="pearson",
            n_observations=1000,
        )
        fit = estimate_half_life(curve)
        assert np.isfinite(fit.half_life)
        assert fit.half_life > 0

    def test_returns_halflifefit(self):
        curve = AlphaDecayCurve(
            horizons=np.array([1, 2, 3]),
            ics=np.array([0.10, 0.05, 0.025]),
            ic_ci_lower=np.full(3, np.nan),
            ic_ci_upper=np.full(3, np.nan),
            method="pearson",
            n_observations=1000,
        )
        fit = estimate_half_life(curve)
        assert isinstance(fit, HalfLifeFit)
        # IC halves every period → half-life ≈ 1 period
        assert abs(fit.half_life - 1.0) < 0.1
        assert fit.r_squared > 0.99  # almost perfect fit

    def test_summary_renders(self):
        curve = AlphaDecayCurve(
            horizons=np.array([1, 2, 3]),
            ics=np.array([0.10, 0.05, 0.025]),
            ic_ci_lower=np.full(3, np.nan),
            ic_ci_upper=np.full(3, np.nan),
            method="pearson",
            n_observations=1000,
        )
        fit = estimate_half_life(curve)
        s = fit.summary()
        assert "HalfLifeFit" in s
        assert "Half-life" in s


# ─── End-to-end ──────────────────────────────────────────────────────


class TestEndToEnd:
    def test_full_pipeline_from_prices(self):
        """Realistic workflow: prices → forward returns → IC curve → half-life."""
        rng = np.random.default_rng(0)
        T = 500
        # Generate prices and a signal that predicts short-term moves
        returns_daily = 0.01 * rng.standard_normal(T)
        prices = 100.0 * np.cumprod(1 + returns_daily)
        # Signal: lagged return (weak predictor at short horizons)
        signal = np.roll(returns_daily, 1)
        signal[0] = 0.0

        horizons = [1, 2, 5, 10]
        fr = make_forward_returns(prices, horizons)
        curve = compute_ic_curve(signal, fr, horizons, method="spearman")
        assert curve.horizons.shape == (4,)
        assert curve.ics.shape == (4,)

        fit = estimate_half_life(curve)
        # Don't assert on the half-life value (lagged-return signal on random
        # data is weak); just verify the API completes
        assert isinstance(fit, HalfLifeFit)
