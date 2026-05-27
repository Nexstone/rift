"""Changepoint detection — structural breaks in a returns / vol series.

Complements `HMMRegimeDetector`: HMM gives you continuous regime labels
based on a learned mixture; changepoint detection answers a different
question — "when did the data-generating process meaningfully shift?"

Both are useful, often together:
  - HMM for *what regime are we in right now* (bull / chop / bear).
  - Changepoints for *when did the last regime break happen* and how
    far back is data still representative of current conditions.

Wraps `ruptures` (PELT with RBF cost by default). Strategy-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import ruptures as rpt


CostModel = Literal["rbf", "l1", "l2", "normal", "linear"]


@dataclass(frozen=True)
class ChangepointResult:
    """Output of detect_changepoints.

    breakpoints:
        Indices (in the input array) where a regime change begins.
        Excludes the trailing endpoint that ruptures appends.
    n_breakpoints:
        Convenience count of breakpoints.
    model:
        Cost model used (rbf / l1 / l2 / normal / linear).
    penalty:
        The penalty value used by PELT.
    n_obs:
        Length of the input series.
    """

    breakpoints: list[int]
    n_breakpoints: int
    model: str
    penalty: float
    n_obs: int


def detect_changepoints(
    series: np.ndarray | list[float],
    model: CostModel = "rbf",
    penalty: float = 10.0,
    min_size: int = 20,
) -> ChangepointResult:
    """Detect structural breaks in a 1D series using PELT.

    Args:
        series: 1D numeric array (returns, log-vol, spread, etc.).
        model: Cost model. "rbf" handles general mean+variance shifts
            (the common default). "l2" is fastest, mean-shift only.
            "normal" is mean+variance under a Gaussian assumption.
        penalty: Penalty for adding a breakpoint. Higher → fewer
            breakpoints. Typical range 5–50; tune for your series.
        min_size: Minimum segment length between breakpoints.

    Returns:
        ChangepointResult with the breakpoint indices.
    """
    arr = np.asarray(series, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"detect_changepoints expects a 1D series, got shape {arr.shape}")
    if arr.size < 2 * min_size:
        # Too short to find any segment of min_size on either side.
        return ChangepointResult(
            breakpoints=[],
            n_breakpoints=0,
            model=model,
            penalty=penalty,
            n_obs=int(arr.size),
        )

    algo = rpt.Pelt(model=model, min_size=min_size).fit(arr)
    raw = algo.predict(pen=penalty)
    # ruptures always returns the trailing endpoint (== len(series)); drop it.
    breakpoints = [int(b) for b in raw if b < arr.size]
    return ChangepointResult(
        breakpoints=breakpoints,
        n_breakpoints=len(breakpoints),
        model=model,
        penalty=penalty,
        n_obs=int(arr.size),
    )


def regime_segments(
    series: np.ndarray | list[float],
    model: CostModel = "rbf",
    penalty: float = 10.0,
    min_size: int = 20,
) -> list[tuple[int, int]]:
    """Convenience: return [(start, end), ...] segments separated by changepoints.

    Each tuple is a half-open interval [start, end) in the input array.
    The last segment ends at len(series).

    Example:
        >>> segs = regime_segments(returns)
        >>> for start, end in segs:
        ...     print(f"regime span: {start}-{end}, mean={returns[start:end].mean():.4f}")
    """
    arr = np.asarray(series, dtype=float)
    result = detect_changepoints(arr, model=model, penalty=penalty, min_size=min_size)
    boundaries = [0, *result.breakpoints, int(arr.size)]
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]
