"""Unit tests for Probabilistic Sharpe Ratio."""

from __future__ import annotations

import numpy as np

from rift_substrate.stats.psr import (
    probabilistic_sharpe_ratio,
    probabilistic_sharpe_ratio_annualized,
)


class TestProbabilisticSharpe:
    def test_observed_equals_threshold_returns_half(self):
        """If observed == threshold, PSR should be 0.5 (no evidence either way)."""
        psr = probabilistic_sharpe_ratio(
            observed_sharpe=0.1, n_observations=100, threshold=0.1,
        )
        assert abs(psr - 0.5) < 0.01

    def test_higher_observed_higher_psr(self):
        """Higher observed Sharpe → higher PSR (more confidence in positive)."""
        psr_lo = probabilistic_sharpe_ratio(
            observed_sharpe=0.05, n_observations=500, threshold=0.0,
        )
        psr_hi = probabilistic_sharpe_ratio(
            observed_sharpe=0.20, n_observations=500, threshold=0.0,
        )
        assert psr_hi > psr_lo

    def test_more_observations_more_certainty(self):
        """More observations → more certain about deviation from threshold."""
        psr_few = probabilistic_sharpe_ratio(
            observed_sharpe=0.1, n_observations=30, threshold=0.0,
        )
        psr_many = probabilistic_sharpe_ratio(
            observed_sharpe=0.1, n_observations=1000, threshold=0.0,
        )
        # With observed > threshold, more samples → more confidence
        assert psr_many > psr_few

    def test_negative_skew_penalizes_observed(self):
        """Negative skew (left tail) makes a positive Sharpe less impressive."""
        psr_symmetric = probabilistic_sharpe_ratio(
            observed_sharpe=0.15, n_observations=500, threshold=0.0,
            skew=0.0, kurtosis=3.0,
        )
        psr_neg_skew = probabilistic_sharpe_ratio(
            observed_sharpe=0.15, n_observations=500, threshold=0.0,
            skew=-1.0, kurtosis=3.0,
        )
        assert psr_neg_skew < psr_symmetric

    def test_fat_tails_reduces_significance(self):
        """High kurtosis (fat tails) makes positive Sharpe less significant."""
        psr_normal = probabilistic_sharpe_ratio(
            observed_sharpe=0.15, n_observations=500, threshold=0.0,
            skew=0.0, kurtosis=3.0,
        )
        psr_fat_tails = probabilistic_sharpe_ratio(
            observed_sharpe=0.15, n_observations=500, threshold=0.0,
            skew=0.0, kurtosis=8.0,
        )
        assert psr_fat_tails < psr_normal

    def test_too_few_observations_returns_nan(self):
        psr = probabilistic_sharpe_ratio(
            observed_sharpe=0.1, n_observations=1, threshold=0.0,
        )
        assert np.isnan(psr)

    def test_psr_in_valid_range(self):
        """PSR is a probability — always in [0, 1]."""
        for obs in [-1.0, -0.1, 0.0, 0.1, 1.0]:
            for n in [10, 100, 1000]:
                psr = probabilistic_sharpe_ratio(
                    observed_sharpe=obs, n_observations=n, threshold=0.0,
                )
                if not np.isnan(psr):
                    assert 0.0 <= psr <= 1.0, f"PSR={psr} out of range for obs={obs}, n={n}"

    def test_annualized_wrapper_consistent(self):
        """Annualized wrapper should give same result as manual conversion."""
        observed_ann = 1.5
        ppy = 365
        # Convert to per-period
        observed_per_period = observed_ann / np.sqrt(ppy)

        psr_direct = probabilistic_sharpe_ratio(
            observed_sharpe=observed_per_period, n_observations=500, threshold=0.0,
        )
        psr_wrapper = probabilistic_sharpe_ratio_annualized(
            observed_sharpe_annualized=observed_ann, n_observations=500,
            periods_per_year=ppy, threshold_annualized=0.0,
        )
        assert abs(psr_direct - psr_wrapper) < 1e-10
