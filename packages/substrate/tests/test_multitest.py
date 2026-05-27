"""Unit tests for multi-comparison corrections."""

from __future__ import annotations

import numpy as np

from rift_substrate.stats.multitest import (
    benjamini_hochberg,
    benjamini_hochberg_adjusted,
    bonferroni,
    holm,
)


class TestBonferroni:
    def test_single_p_unchanged(self):
        np.testing.assert_allclose(bonferroni([0.03]), [0.03])

    def test_multiplies_by_count(self):
        # 5 tests at p=0.01 each → adjusted 0.05
        adj = bonferroni([0.01, 0.01, 0.01, 0.01, 0.01])
        np.testing.assert_allclose(adj, [0.05] * 5)

    def test_caps_at_one(self):
        # p * M > 1 should clip to 1
        adj = bonferroni([0.5, 0.5, 0.5])
        np.testing.assert_allclose(adj, [1.0, 1.0, 1.0])

    def test_empty_input(self):
        adj = bonferroni([])
        assert adj.size == 0


class TestHolm:
    def test_single_p_unchanged(self):
        np.testing.assert_allclose(holm([0.04]), [0.04])

    def test_holm_more_powerful_than_bonferroni(self):
        """For the smallest p-value, Holm and Bonferroni multiply by M.
        For larger p-values, Holm divides by smaller numbers → smaller adjusted.
        So Holm is uniformly <= Bonferroni."""
        ps = [0.01, 0.04, 0.03, 0.02]
        b = bonferroni(ps)
        h = holm(ps)
        # Smallest p → both multiply by M
        smallest_idx = int(np.argmin(ps))
        assert h[smallest_idx] == b[smallest_idx]
        # Others: Holm should be <= Bonferroni
        for i in range(len(ps)):
            assert h[i] <= b[i] + 1e-10

    def test_monotonicity_preserved(self):
        """When p-values are sorted, adjusted should also be sorted."""
        ps = [0.001, 0.01, 0.02, 0.04, 0.05]
        h = holm(ps)
        # In original order (already sorted), h should be non-decreasing
        for i in range(len(h) - 1):
            assert h[i] <= h[i + 1] + 1e-10

    def test_caps_at_one(self):
        h = holm([0.5, 0.5, 0.5, 0.5, 0.5])
        assert all(v <= 1.0 for v in h)


class TestBenjaminiHochberg:
    def test_rejects_clear_signals(self):
        """Very small p-values should be rejected (discoveries)."""
        ps = [0.001, 0.002, 0.5, 0.6, 0.7]
        rejected = benjamini_hochberg(ps, fdr=0.05)
        # The two tiny p-values should be discoveries
        assert rejected[0]
        assert rejected[1]
        # The large ones should not
        assert not rejected[2]
        assert not rejected[3]
        assert not rejected[4]

    def test_rejects_none_when_all_high(self):
        rejected = benjamini_hochberg([0.5, 0.6, 0.7], fdr=0.05)
        assert not rejected.any()

    def test_preserves_original_order(self):
        ps_unsorted = [0.6, 0.001, 0.7, 0.002, 0.5]
        rejected = benjamini_hochberg(ps_unsorted, fdr=0.05)
        # Discoveries at original positions 1 and 3
        assert rejected[1]
        assert rejected[3]
        assert not rejected[0]
        assert not rejected[2]
        assert not rejected[4]


class TestBenjaminiHochbergAdjusted:
    def test_q_values_in_valid_range(self):
        ps = [0.001, 0.01, 0.03, 0.5, 0.9]
        q = benjamini_hochberg_adjusted(ps)
        assert all(0 <= v <= 1 for v in q)

    def test_q_values_smaller_than_or_equal_to_one(self):
        q = benjamini_hochberg_adjusted([0.5, 0.6, 0.7])
        assert all(v <= 1.0 for v in q)

    def test_monotonicity_in_sorted_order(self):
        """When sorted by original p, q-values should also be non-decreasing."""
        ps = np.array([0.001, 0.01, 0.03, 0.05, 0.5])
        q = benjamini_hochberg_adjusted(ps)
        for i in range(len(q) - 1):
            assert q[i] <= q[i + 1] + 1e-10
