"""Unit tests for stationary block bootstrap."""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.stats.bootstrap import (
    optimal_block_size,
    stationary_bootstrap,
)


class TestOptimalBlockSize:
    def test_iid_series_returns_small_block(self):
        """IID series has no autocorrelation; block size should be small."""
        rng = np.random.default_rng(42)
        x = rng.normal(0, 1, size=1000)
        b = optimal_block_size(x)
        assert 2 <= b <= 50, f"IID series got block size {b}, expected small"

    def test_ar1_series_returns_larger_block(self):
        """Strongly autocorrelated AR(1) needs larger blocks."""
        rng = np.random.default_rng(42)
        n = 2000
        x = np.zeros(n)
        x[0] = rng.normal()
        for i in range(1, n):
            x[i] = 0.9 * x[i - 1] + rng.normal()
        b = optimal_block_size(x)
        # AR(1) with ρ=0.9 has long-range dependence; block should be > 5
        assert b >= 5, f"AR(1) series got block size {b}, expected >= 5"

    def test_short_series_returns_at_least_2(self):
        b = optimal_block_size([1.0, 2.0, 3.0, 4.0])
        assert b >= 2

    def test_constant_series_returns_2(self):
        """No variance → return minimum block size."""
        b = optimal_block_size([1.0] * 100)
        assert b == 2

    def test_rejects_2d_input(self):
        with pytest.raises(ValueError, match="1-D"):
            optimal_block_size(np.array([[1, 2], [3, 4]]))

    def test_rejects_tiny_input(self):
        with pytest.raises(ValueError, match="at least 2"):
            optimal_block_size([1.0])


class TestStationaryBootstrap:
    def test_output_shape(self):
        x = np.arange(100, dtype=float)
        out = stationary_bootstrap(x, n_resamples=50, avg_block_size=5, seed=1)
        assert out.shape == (50, 100)

    def test_reproducible_with_seed(self):
        x = np.arange(100, dtype=float)
        a = stationary_bootstrap(x, n_resamples=10, avg_block_size=5, seed=42)
        b = stationary_bootstrap(x, n_resamples=10, avg_block_size=5, seed=42)
        np.testing.assert_array_equal(a, b)

    def test_different_seeds_differ(self):
        x = np.arange(100, dtype=float)
        a = stationary_bootstrap(x, n_resamples=10, avg_block_size=5, seed=1)
        b = stationary_bootstrap(x, n_resamples=10, avg_block_size=5, seed=2)
        assert not np.array_equal(a, b)

    def test_resamples_contain_only_original_values(self):
        """Bootstrap draws from the original series — every resampled
        value must be present in the original."""
        x = np.arange(100, dtype=float)
        out = stationary_bootstrap(x, n_resamples=20, avg_block_size=10, seed=1)
        original_set = set(x.tolist())
        for row in out:
            assert set(row.tolist()).issubset(original_set)

    def test_mean_of_resamples_approximates_population_mean(self):
        """For a stationary series, the mean of resampled-series-means
        should be close to the population mean."""
        rng = np.random.default_rng(42)
        x = rng.normal(5.0, 2.0, size=500)
        out = stationary_bootstrap(x, n_resamples=500, avg_block_size=10, seed=7)
        resample_means = out.mean(axis=1)
        # Mean of bootstrap means should be very close to original mean
        assert abs(resample_means.mean() - x.mean()) < 0.1

    def test_block_size_one_is_iid_bootstrap(self):
        """avg_block_size=1 reduces to IID bootstrap (geometric(1) → length 1)."""
        x = np.arange(100, dtype=float)
        out = stationary_bootstrap(x, n_resamples=10, avg_block_size=1, seed=1)
        # All resampled values should be valid indices of original
        assert out.shape == (10, 100)
        for row in out:
            for v in row:
                assert 0 <= v <= 99

    def test_auto_block_size_used_when_none(self):
        x = np.arange(100, dtype=float)
        out = stationary_bootstrap(x, n_resamples=5, avg_block_size=None, seed=1)
        assert out.shape == (5, 100)

    def test_rejects_invalid_block_size(self):
        with pytest.raises(ValueError, match="avg_block_size"):
            stationary_bootstrap([1.0, 2.0, 3.0], avg_block_size=0)
