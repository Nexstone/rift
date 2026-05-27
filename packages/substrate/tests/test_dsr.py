"""Unit tests for Deflated Sharpe Ratio."""

from __future__ import annotations

import numpy as np

from rift_substrate.stats.dsr import (
    deflated_sharpe_ratio,
    deflated_sharpe_ratio_from_trial_results,
    expected_max_sharpe_under_null,
)
from rift_substrate.stats.psr import probabilistic_sharpe_ratio


class TestExpectedMaxUnderNull:
    def test_single_trial_zero_threshold(self):
        """1 trial with zero variance → 0 expected max."""
        em = expected_max_sharpe_under_null(n_trials=1, variance_of_trial_sharpes=1.0)
        assert em == 0.0

    def test_more_trials_higher_expected_max(self):
        """More trials → higher expected max of trial Sharpes under null."""
        var = 0.04  # std = 0.2
        em_10 = expected_max_sharpe_under_null(10, var)
        em_100 = expected_max_sharpe_under_null(100, var)
        em_1000 = expected_max_sharpe_under_null(1000, var)
        assert em_10 < em_100 < em_1000

    def test_higher_variance_higher_expected_max(self):
        em_low = expected_max_sharpe_under_null(100, 0.01)
        em_high = expected_max_sharpe_under_null(100, 0.10)
        assert em_high > em_low

    def test_zero_variance_returns_zero(self):
        em = expected_max_sharpe_under_null(100, 0.0)
        assert em == 0.0


class TestDeflatedSharpe:
    def test_single_trial_reduces_to_psr(self):
        """With 1 trial (no selection), DSR == PSR(threshold=0)."""
        dsr = deflated_sharpe_ratio(
            observed_sharpe=0.15, n_observations=500,
            n_trials=1, variance_of_trial_sharpes=0.04,
        )
        psr = probabilistic_sharpe_ratio(
            observed_sharpe=0.15, n_observations=500, threshold=0.0,
        )
        assert abs(dsr - psr) < 1e-10

    def test_many_trials_reduces_dsr(self):
        """Same observed Sharpe but more trials → lower DSR (selection bias)."""
        dsr_few = deflated_sharpe_ratio(
            observed_sharpe=0.20, n_observations=500,
            n_trials=2, variance_of_trial_sharpes=0.04,
        )
        dsr_many = deflated_sharpe_ratio(
            observed_sharpe=0.20, n_observations=500,
            n_trials=1000, variance_of_trial_sharpes=0.04,
        )
        assert dsr_many < dsr_few

    def test_dsr_in_valid_range(self):
        """DSR is a probability — always in [0, 1]."""
        for n_trials in [1, 10, 100, 1000]:
            for var in [0.0, 0.04, 0.10]:
                dsr = deflated_sharpe_ratio(
                    observed_sharpe=0.10, n_observations=500,
                    n_trials=n_trials, variance_of_trial_sharpes=var,
                )
                if not np.isnan(dsr):
                    assert 0 <= dsr <= 1, f"DSR={dsr} out of range"

    def test_from_trial_results_takes_max(self):
        """Convenience wrapper picks the max trial Sharpe as the observed."""
        trial_sharpes = [0.05, 0.10, 0.15, 0.08, 0.20]
        dsr = deflated_sharpe_ratio_from_trial_results(
            trial_sharpes=trial_sharpes, n_observations_per_trial=500,
        )
        # Compare to manual call with observed=0.20, n_trials=5
        manual = deflated_sharpe_ratio(
            observed_sharpe=0.20, n_observations=500,
            n_trials=5, variance_of_trial_sharpes=float(np.var(trial_sharpes, ddof=1)),
        )
        assert abs(dsr - manual) < 1e-10
