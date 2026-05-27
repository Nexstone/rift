"""Tests for substrate.validation — purged k-fold + combinatorial CV.

Pins the López de Prado §7 invariants:
  1. Train and test indices never overlap (basic k-fold property)
  2. Training labels don't overlap with test fold window (purging works)
  3. Embargo gap is enforced after each test fold
  4. CombinatoriallyPurgedCV produces C(N, k) splits
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.validation import (
    CombinatoriallyPurgedCV,
    PurgedKFold,
    embargo_times,
    purge_train_indices,
)


# ─── embargo_times ────────────────────────────────────────────────────


class TestEmbargoTimes:
    def test_single_end_time(self):
        all_t = np.arange(100, dtype=np.float64)
        result = embargo_times(50.0, all_t, embargo_pct=0.01)
        # 1% of [0, 99] = 0.99
        assert result[0] == pytest.approx(50.0 + 0.99)

    def test_array_end_times(self):
        all_t = np.arange(100, dtype=np.float64)
        ends = np.array([50.0, 70.0, 90.0])
        result = embargo_times(ends, all_t, embargo_pct=0.05)
        # 5% of [0, 99] = 4.95
        np.testing.assert_allclose(result, [54.95, 74.95, 94.95])

    def test_zero_embargo(self):
        all_t = np.arange(100, dtype=np.float64)
        result = embargo_times(50.0, all_t, embargo_pct=0.0)
        assert result[0] == 50.0

    def test_tiny_dataset(self):
        # < 2 elements → no embargo
        result = embargo_times(50.0, np.array([50.0]), embargo_pct=0.5)
        assert result[0] == 50.0


# ─── purge_train_indices ──────────────────────────────────────────────


class TestPurgeTrainIndices:
    def test_purges_overlapping_labels(self):
        """Sample with label-end inside test window must be purged."""
        n = 20
        t0 = np.arange(n, dtype=np.float64)
        t1 = t0 + 5  # 5-period forward labels

        test_idx = np.array([10, 11, 12])  # test on indices 10-12
        train_idx = np.array([5, 6, 7, 8, 9, 13, 14, 15, 16, 17, 18, 19])
        # Sample 7 has label_end = 12 → overlaps test window → should purge
        # Sample 13 has label_start = 13, label_end = 18; test window is [t0=10, t1=17 after embargo]
        # → 13 might also overlap

        purged = purge_train_indices(train_idx, test_idx, t0, t1, embargo_pct=0.0)
        # Check no remaining train sample has [t0, t1] overlapping [10, 17]
        for idx in purged:
            label_start = t0[idx]
            label_end = t1[idx]
            test_window_start = t0[test_idx].min()
            test_window_end = t1[test_idx].max()
            assert label_end < test_window_start or label_start > test_window_end

    def test_embargo_drops_post_test_samples(self):
        n = 100
        t0 = np.arange(n, dtype=np.float64)
        t1 = t0  # point-in-time labels (no forward window)

        test_idx = np.array([40, 41, 42])
        train_idx = np.arange(n)
        train_idx = train_idx[~np.isin(train_idx, test_idx)]

        # 1% embargo on 100-point span = 0.99 ≈ 1 sample
        purged = purge_train_indices(train_idx, test_idx, t0, t1, embargo_pct=0.05)
        # Embargo: 5% × 99 = 4.95. So train samples with t0 in (42, 47] are dropped.
        for idx in purged:
            label_start = t0[idx]
            label_end = t1[idx]
            if label_start > 42:  # after test fold
                assert label_start > 42 + 4.95

    def test_empty_test_set_returns_train_unchanged(self):
        train_idx = np.array([0, 1, 2, 3])
        t0 = np.arange(10, dtype=np.float64)
        t1 = t0 + 1
        result = purge_train_indices(train_idx, np.array([], dtype=int), t0, t1)
        np.testing.assert_array_equal(result, train_idx)


# ─── PurgedKFold ──────────────────────────────────────────────────────


class TestPurgedKFold:
    def _make_data(self, n: int = 100, forward_window: int = 5):
        X = np.arange(n).reshape(-1, 1)
        t0 = np.arange(n, dtype=np.float64)
        t1 = t0 + forward_window
        return X, t0, t1

    def test_n_splits_must_be_at_least_2(self):
        with pytest.raises(ValueError, match="n_splits"):
            PurgedKFold(n_splits=1)

    def test_embargo_pct_must_be_in_range(self):
        with pytest.raises(ValueError, match="embargo_pct"):
            PurgedKFold(embargo_pct=-0.01)
        with pytest.raises(ValueError, match="embargo_pct"):
            PurgedKFold(embargo_pct=1.5)

    def test_basic_partition(self):
        X, t0, t1 = self._make_data(n=100, forward_window=5)
        cv = PurgedKFold(n_splits=5, embargo_pct=0.01)
        folds = list(cv.split(X, t1, t0))
        assert len(folds) == 5

    def test_test_folds_disjoint(self):
        X, t0, t1 = self._make_data(n=100, forward_window=5)
        cv = PurgedKFold(n_splits=5, embargo_pct=0.01)
        all_test = []
        for _, test_idx in cv.split(X, t1, t0):
            all_test.append(test_idx)
        # Concatenated test indices cover all samples without overlap
        flat = np.concatenate(all_test)
        assert len(flat) == 100
        assert len(np.unique(flat)) == 100  # disjoint

    def test_no_train_test_overlap(self):
        X, t0, t1 = self._make_data(n=200, forward_window=10)
        cv = PurgedKFold(n_splits=5, embargo_pct=0.02)
        for train_idx, test_idx in cv.split(X, t1, t0):
            overlap = np.intersect1d(train_idx, test_idx)
            assert overlap.size == 0

    def test_purging_drops_label_overlap(self):
        X, t0, t1 = self._make_data(n=200, forward_window=20)
        cv = PurgedKFold(n_splits=5, embargo_pct=0.0)  # no embargo, just purge
        for train_idx, test_idx in cv.split(X, t1, t0):
            # No train sample's label spans the test window
            test_window_start = t0[test_idx].min()
            test_window_end = t1[test_idx].max()
            for idx in train_idx:
                assert t1[idx] < test_window_start or t0[idx] > test_window_end

    def test_t1_length_mismatch_raises(self):
        X = np.arange(100).reshape(-1, 1)
        cv = PurgedKFold(n_splits=5)
        with pytest.raises(ValueError, match="length"):
            next(cv.split(X, np.arange(50, dtype=np.float64)))


# ─── CombinatoriallyPurgedCV ─────────────────────────────────────────


class TestCombinatoriallyPurgedCV:
    def _make_data(self, n: int = 100):
        X = np.arange(n).reshape(-1, 1)
        t0 = np.arange(n, dtype=np.float64)
        t1 = t0 + 5
        return X, t0, t1

    def test_n_paths_matches_combinatorial(self):
        cv = CombinatoriallyPurgedCV(n_splits=10, n_test_splits=2)
        assert cv.n_paths == 45  # C(10, 2)

        cv = CombinatoriallyPurgedCV(n_splits=8, n_test_splits=3)
        assert cv.n_paths == 56  # C(8, 3)

    def test_generates_all_combinations(self):
        X, t0, t1 = self._make_data(n=100)
        cv = CombinatoriallyPurgedCV(n_splits=10, n_test_splits=2, embargo_pct=0.0)
        splits = list(cv.split(X, t1, t0))
        assert len(splits) == 45

    def test_each_split_has_disjoint_train_test(self):
        X, t0, t1 = self._make_data(n=100)
        cv = CombinatoriallyPurgedCV(n_splits=10, n_test_splits=2, embargo_pct=0.01)
        for train_idx, test_idx in cv.split(X, t1, t0):
            assert np.intersect1d(train_idx, test_idx).size == 0

    def test_test_size_is_k_folds(self):
        """For 100 samples, 10 folds, 2 test → test size = 20."""
        X, t0, t1 = self._make_data(n=100)
        cv = CombinatoriallyPurgedCV(n_splits=10, n_test_splits=2, embargo_pct=0.0)
        for _, test_idx in cv.split(X, t1, t0):
            assert len(test_idx) == 20

    def test_n_test_splits_must_be_valid(self):
        with pytest.raises(ValueError, match="n_test_splits"):
            CombinatoriallyPurgedCV(n_splits=5, n_test_splits=0)
        with pytest.raises(ValueError, match="n_test_splits"):
            CombinatoriallyPurgedCV(n_splits=5, n_test_splits=5)  # = n_splits
