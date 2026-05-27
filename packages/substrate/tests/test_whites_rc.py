"""Unit tests for White's Reality Check."""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.stats.whites_rc import (
    RealityCheckResult,
    whites_reality_check,
)


class TestWhitesRealityCheck:
    def test_one_clearly_winning_strategy_has_low_pvalue(self):
        """A strategy that clearly beats baseline should yield small p-value."""
        rng = np.random.default_rng(42)
        n = 500
        baseline = rng.normal(0, 0.01, size=n)
        # Strategy 0 has mean 0.005 (clear edge), others are baseline-like
        winning = baseline + 0.005
        loser1 = baseline + rng.normal(0, 0.001, size=n)
        loser2 = baseline + rng.normal(0, 0.001, size=n)

        res = whites_reality_check(
            strategy_returns=[winning, loser1, loser2],
            baseline_returns=baseline,
            n_bootstrap=200,
            seed=1,
        )
        assert isinstance(res, RealityCheckResult)
        assert res.best_strategy_idx == 0
        assert res.p_value < 0.05  # significant

    def test_all_noise_strategies_high_pvalue(self):
        """Strategies that are pure noise vs baseline shouldn't pass."""
        rng = np.random.default_rng(42)
        n = 500
        baseline = rng.normal(0, 0.01, size=n)
        # 20 pure-noise strategies — best should not be significant
        noise_strats = [baseline + rng.normal(0, 0.001, size=n) for _ in range(20)]

        res = whites_reality_check(
            strategy_returns=noise_strats,
            baseline_returns=baseline,
            n_bootstrap=200,
            seed=1,
        )
        # With pure noise vs same baseline, p-value should be high (>0.1)
        assert res.p_value > 0.1, f"got p={res.p_value} for noise; expected >0.1"

    def test_works_with_2d_array_input(self):
        """Accept 2D (T, K) array form, not just list."""
        rng = np.random.default_rng(42)
        n = 200
        baseline = rng.normal(0, 0.01, size=n)
        matrix = np.column_stack([baseline + 0.001, baseline - 0.001, baseline])

        res = whites_reality_check(
            strategy_returns=matrix,
            baseline_returns=baseline,
            n_bootstrap=100,
            seed=1,
        )
        assert res.n_strategies == 3
        assert res.n_observations == n

    def test_returns_bootstrap_distribution(self):
        rng = np.random.default_rng(42)
        baseline = rng.normal(0, 0.01, size=200)
        strats = [baseline + 0.001, baseline - 0.001]
        res = whites_reality_check(
            strategy_returns=strats,
            baseline_returns=baseline,
            n_bootstrap=100,
            seed=1,
        )
        assert res.bootstrap_max_distribution.shape == (100,)

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError):
            whites_reality_check(
                strategy_returns=[np.zeros(100), np.zeros(50)],
                baseline_returns=np.zeros(100),
                n_bootstrap=10,
            )

    def test_significant_flags_correct(self):
        # Build a case with extremely strong out-performance
        rng = np.random.default_rng(42)
        baseline = rng.normal(0, 0.005, size=500)
        winner = baseline + 0.01  # huge edge
        res = whites_reality_check(
            strategy_returns=[winner],
            baseline_returns=baseline,
            n_bootstrap=200,
            seed=1,
        )
        assert res.significant_at_5pct
