"""Purged k-fold cross-validation (López de Prado AFML §7).

Problem with naive k-fold in finance:
  - Labels span time. For example, "30-day-forward return at time t" needs
    data from t to t+30. If you train on t and test on t+5, the training
    label leaks data that the test fold would otherwise predict.
  - Returns are serially correlated. Even non-overlapping splits have leakage
    from autocorrelation; the LdP fix is an "embargo" period that drops
    training data immediately AFTER the test fold to break that leakage.

Solution (PurgedKFold):
  1. Partition observation indices into k equal-size contiguous folds.
  2. For each fold as the test set:
     a. PURGE training samples whose [label_start, label_end] overlaps
        the test fold's [start, end].
     b. EMBARGO an additional `embargo_pct × N` samples after the test fold
        from training (drops the next h indices).
  3. The remaining training samples form the train set for this fold.

Combinatorial variant (CombinatoriallyPurgedCV):
  - Choose any k test folds out of N total folds (instead of just 1).
  - Generate all C(N, k) such combinations.
  - For each, purge + embargo as above.
  - Yields many more train/test paths than ordinary k-fold; aggregating
    OOS Sharpe over these paths is the AFML §12 "multi-path bootstrap"
    OOS estimator — currently the most rigorous publicly-documented method.

Usage:

    from rift_substrate.validation import PurgedKFold

    # `t1` is a Series-like: for each observation, the END timestamp of its label.
    # (For point-in-time labels, set t1[i] = index[i].)
    cv = PurgedKFold(n_splits=5, embargo_pct=0.01)
    for train_idx, test_idx in cv.split(X, t1):
        model.fit(X[train_idx])
        score = model.score(X[test_idx])
"""

from __future__ import annotations

from itertools import combinations
from typing import Iterator

import numpy as np
from numpy.typing import NDArray


# ─── Core helpers ──────────────────────────────────────────────────────


def embargo_times(
    test_end_t: NDArray | float,
    all_t: NDArray,
    embargo_pct: float = 0.01,
) -> NDArray:
    """Return the embargo cutoff time per AFML §7.

    After the test fold ends at time `test_end_t`, training data with
    label-start times in [test_end_t, test_end_t + embargo] is dropped.
    `embargo` is `embargo_pct × (max(all_t) - min(all_t))`.

    Args:
      test_end_t:   single time or array of test-fold end times
      all_t:        all event times in the dataset (used to size the embargo)
      embargo_pct:  fraction of the total time range to embargo (default 1%)

    Returns:
      Array of embargo cutoffs (one per element of `test_end_t`).
    """
    all_t_arr = np.asarray(all_t)
    if all_t_arr.size < 2:
        return np.atleast_1d(test_end_t).astype(np.float64)
    span = float(np.ptp(all_t_arr.astype(np.float64)))
    embargo_window = embargo_pct * span
    return np.atleast_1d(test_end_t).astype(np.float64) + embargo_window


def purge_train_indices(
    train_idx: NDArray,
    test_idx: NDArray,
    t0: NDArray,
    t1: NDArray,
    embargo_pct: float = 0.01,
) -> NDArray:
    """Remove training samples that overlap with the test fold.

    A training sample i is purged if its label window [t0[i], t1[i]] overlaps
    with the test fold's window [min(t0[test_idx]), max(t1[test_idx]) + embargo].

    Args:
      train_idx:    candidate training indices
      test_idx:     test indices for this fold
      t0:           label-start times for all samples
      t1:           label-end times for all samples
      embargo_pct:  fraction of total time to embargo after test fold

    Returns:
      Filtered training indices with overlapping/embargoed samples removed.
    """
    if test_idx.size == 0:
        return train_idx
    t0_arr = np.asarray(t0, dtype=np.float64)
    t1_arr = np.asarray(t1, dtype=np.float64)

    test_window_start = float(t0_arr[test_idx].min())
    test_window_end = float(t1_arr[test_idx].max())
    embargo_end = float(embargo_times(test_window_end, t0_arr, embargo_pct)[0])

    # A train sample overlaps the test+embargo window if its [t0_i, t1_i]
    # intersects [test_start, embargo_end].
    train_t0 = t0_arr[train_idx]
    train_t1 = t1_arr[train_idx]
    no_overlap = (train_t1 < test_window_start) | (train_t0 > embargo_end)
    return train_idx[no_overlap]


# ─── PurgedKFold ─────────────────────────────────────────────────────


class PurgedKFold:
    """Walk-forward-ish k-fold with purge + embargo.

    Standard k-fold partitions indices into `n_splits` contiguous folds.
    For each fold as test, training is all OTHER samples — but purged of
    label-overlapping training samples and with an embargo gap after the
    test fold.

    Args:
      n_splits:     number of folds (default 5)
      embargo_pct:  embargo gap as fraction of total time span (default 0.01 = 1%)

    `split(X, t1, t0=None)` yields `(train_idx, test_idx)` for each fold.
    `t1[i]` is the label-end time for sample i. If labels are point-in-time
    (no forward window), pass `t1` equal to the index times. `t0` defaults
    to `t1` when not provided.
    """

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01):
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2; got {n_splits}")
        if not 0 <= embargo_pct < 1:
            raise ValueError(f"embargo_pct must be in [0, 1); got {embargo_pct}")
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct

    def split(
        self,
        X: NDArray,
        t1: NDArray,
        t0: NDArray | None = None,
    ) -> Iterator[tuple[NDArray, NDArray]]:
        n_samples = len(X)
        if n_samples != len(t1):
            raise ValueError(
                f"X length ({n_samples}) != t1 length ({len(t1)})"
            )
        if t0 is None:
            t0 = np.asarray(t1)
        else:
            t0 = np.asarray(t0)
            if len(t0) != n_samples:
                raise ValueError(f"t0 length ({len(t0)}) != X length ({n_samples})")

        all_idx = np.arange(n_samples)
        # Equal-size contiguous folds (assuming t1 is time-ordered)
        fold_boundaries = np.linspace(0, n_samples, self.n_splits + 1, dtype=int)

        for k in range(self.n_splits):
            test_start, test_end = fold_boundaries[k], fold_boundaries[k + 1]
            test_idx = all_idx[test_start:test_end]
            train_idx_raw = np.concatenate([all_idx[:test_start], all_idx[test_end:]])
            train_idx = purge_train_indices(
                train_idx_raw, test_idx, t0, t1, embargo_pct=self.embargo_pct
            )
            yield train_idx, test_idx


# ─── Combinatorially Purged CV ────────────────────────────────────────


class CombinatoriallyPurgedCV:
    """AFML §12 — all C(N, k) train/test partitions with purge + embargo.

    Generates many more train/test paths than PurgedKFold. Each iteration
    yields one path's (train_idx, test_idx). Aggregating OOS metrics
    across all paths produces a more robust OOS estimate than a single
    walk-forward run.

    Args:
      n_splits:        total number of folds N (default 10)
      n_test_splits:   number of test folds k per partition (default 2)
      embargo_pct:     embargo gap as fraction of total time span (default 0.01)

    Yields C(N, k) splits. For default N=10, k=2: 45 paths.
    """

    def __init__(
        self,
        n_splits: int = 10,
        n_test_splits: int = 2,
        embargo_pct: float = 0.01,
    ):
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2; got {n_splits}")
        if not 1 <= n_test_splits < n_splits:
            raise ValueError(
                f"n_test_splits must be in [1, n_splits); got {n_test_splits}"
            )
        if not 0 <= embargo_pct < 1:
            raise ValueError(f"embargo_pct must be in [0, 1); got {embargo_pct}")
        self.n_splits = n_splits
        self.n_test_splits = n_test_splits
        self.embargo_pct = embargo_pct

    def split(
        self,
        X: NDArray,
        t1: NDArray,
        t0: NDArray | None = None,
    ) -> Iterator[tuple[NDArray, NDArray]]:
        n_samples = len(X)
        if n_samples != len(t1):
            raise ValueError(f"X length ({n_samples}) != t1 length ({len(t1)})")
        if t0 is None:
            t0 = np.asarray(t1)
        else:
            t0 = np.asarray(t0)

        all_idx = np.arange(n_samples)
        fold_boundaries = np.linspace(0, n_samples, self.n_splits + 1, dtype=int)
        # Indices of each fold
        folds = [
            all_idx[fold_boundaries[k]:fold_boundaries[k + 1]]
            for k in range(self.n_splits)
        ]

        # For each combination of n_test_splits folds chosen as test set
        for test_fold_ids in combinations(range(self.n_splits), self.n_test_splits):
            test_idx = np.concatenate([folds[i] for i in test_fold_ids])
            train_idx_raw = np.concatenate([
                folds[i] for i in range(self.n_splits) if i not in test_fold_ids
            ])
            train_idx = purge_train_indices(
                train_idx_raw, test_idx, t0, t1, embargo_pct=self.embargo_pct
            )
            yield train_idx, test_idx

    @property
    def n_paths(self) -> int:
        """Number of paths this CV will generate."""
        from math import comb
        return comb(self.n_splits, self.n_test_splits)
