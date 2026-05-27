"""Validation — out-of-sample testing primitives with correct bias handling.

Backtest cross-validation in finance is harder than in classical ML because
returns are serially correlated and information leaks across folds when
labels span time (e.g., "30-day-forward return" overlaps across consecutive
training periods). Naive k-fold or train/test split overstates out-of-sample
performance dramatically.

This module provides:

  PurgedKFold(n_splits, embargo_pct)
      López de Prado AFML §7 — k-fold where train samples whose labels
      overlap with the test fold are PURGED, and an embargo gap is dropped
      from training after the test fold to prevent serial-correlation leakage.

  CombinatoriallyPurgedCV(n_splits, n_test_splits, embargo_pct)
      Same purging + embargo, but generates ALL C(N, k) combinations of
      train/test fold partitions. Produces the multi-path bootstrap estimate
      from AFML §12 — the most rigorous OOS Sharpe estimator known.

Reference:
  López de Prado, M. (2018). "Advances in Financial Machine Learning."
    Wiley. Chapters 7 and 12.
"""

from rift_substrate.validation.purged_cv import (
    CombinatoriallyPurgedCV,
    PurgedKFold,
    embargo_times,
    purge_train_indices,
)

__all__ = [
    "CombinatoriallyPurgedCV",
    "PurgedKFold",
    "embargo_times",
    "purge_train_indices",
]
