"""Multi-comparison corrections.

When you run M hypothesis tests at significance level α (e.g., comparing
M strategies pairwise), the chance of at least one false positive grows
with M. These corrections adjust to control either:

  - Family-Wise Error Rate (FWER): P(any false positive)
    Bonferroni, Holm — conservative; appropriate for high-stakes decisions
  - False Discovery Rate (FDR): expected fraction of false positives
    among rejections
    Benjamini-Hochberg — less conservative; appropriate for exploratory work

Use:
  - Bonferroni:        max protection, lowest power
  - Holm:              Bonferroni's strict improvement (uniformly more powerful)
  - Benjamini-Hochberg: best for "I want to find SOME real signals among many"

All functions return adjusted p-values (or rejection decisions) in the
same order as the input p-values.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def bonferroni(p_values: list[float] | NDArray) -> NDArray[np.float64]:
    """Bonferroni correction: adjusted_p = min(1, p * M).

    Strongest FWER control, lowest power. Use when even one false
    positive would be catastrophic.

    Returns adjusted p-values; reject if adjusted_p < α.
    """
    p = np.asarray(p_values, dtype=np.float64)
    if p.size == 0:
        return p
    return np.minimum(1.0, p * p.size)


def holm(p_values: list[float] | NDArray) -> NDArray[np.float64]:
    """Holm-Bonferroni (Holm 1979): uniformly more powerful than Bonferroni
    while controlling FWER at the same level.

    Algorithm:
      1. Sort p-values ascending
      2. Multiply each by (M - rank), with rank 0-indexed
      3. Enforce monotonicity: each adjusted_p >= the previous one

    Returns adjusted p-values in original order; reject if adjusted_p < α.
    """
    p = np.asarray(p_values, dtype=np.float64)
    M = p.size
    if M == 0:
        return p

    order = np.argsort(p)
    sorted_p = p[order]

    # Holm step: adjusted_p[i] = (M - i) * sorted_p[i] for i = 0..M-1
    multipliers = np.arange(M, 0, -1, dtype=np.float64)
    adjusted_sorted = sorted_p * multipliers

    # Enforce monotonicity (running max)
    adjusted_sorted = np.maximum.accumulate(adjusted_sorted)
    adjusted_sorted = np.minimum(adjusted_sorted, 1.0)

    # Unsort back to original order
    adjusted = np.empty(M, dtype=np.float64)
    adjusted[order] = adjusted_sorted
    return adjusted


def benjamini_hochberg(
    p_values: list[float] | NDArray,
    fdr: float = 0.05,
) -> NDArray[np.bool_]:
    """Benjamini-Hochberg (1995): controls False Discovery Rate.

    Less conservative than Bonferroni/Holm. Appropriate when finding
    SOME real signals matters more than avoiding any false positives.

    Algorithm:
      1. Sort p-values ascending: p_(1) <= p_(2) <= ... <= p_(M)
      2. Find largest k such that p_(k) <= (k / M) * fdr
      3. Reject hypotheses 1..k (sorted), accept the rest

    Returns boolean array (True = reject null = "discovery") in original
    order.
    """
    p = np.asarray(p_values, dtype=np.float64)
    M = p.size
    if M == 0:
        return np.array([], dtype=bool)

    order = np.argsort(p)
    sorted_p = p[order]

    # Find largest k where sorted_p[k] <= ((k+1) / M) * fdr (1-indexed k+1)
    thresholds = (np.arange(1, M + 1) / M) * fdr
    below = sorted_p <= thresholds
    if not below.any():
        # No discoveries
        decisions = np.zeros(M, dtype=bool)
    else:
        # Reject all p_(i) for i <= max k where sorted_p[k] <= threshold[k]
        k_max = int(np.where(below)[0].max())
        decisions_sorted = np.zeros(M, dtype=bool)
        decisions_sorted[: k_max + 1] = True
        decisions = np.empty(M, dtype=bool)
        decisions[order] = decisions_sorted

    return decisions


def benjamini_hochberg_adjusted(
    p_values: list[float] | NDArray,
) -> NDArray[np.float64]:
    """BH-adjusted p-values (q-values).

    For each i, q_i is the smallest FDR threshold at which test i would
    be rejected. Standard way to report BH-corrected results.

    Returns adjusted p-values in original order.
    """
    p = np.asarray(p_values, dtype=np.float64)
    M = p.size
    if M == 0:
        return p

    order = np.argsort(p)
    sorted_p = p[order]

    # q_(i) = sorted_p[i] * M / (i+1)  for 1-indexed i+1
    ranks = np.arange(1, M + 1, dtype=np.float64)
    q_sorted = sorted_p * M / ranks

    # Enforce monotonicity going right-to-left (each q >= the next one)
    q_sorted = np.minimum.accumulate(q_sorted[::-1])[::-1]
    q_sorted = np.minimum(q_sorted, 1.0)

    q = np.empty(M, dtype=np.float64)
    q[order] = q_sorted
    return q
