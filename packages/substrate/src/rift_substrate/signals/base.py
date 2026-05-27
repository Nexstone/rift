"""Signal panel primitives — the data layer the combiner operates on.

`SignalScorePanel` is the substrate-side analog of `ReturnsPanel` (Phase 2a)
for signal scores. Each column is one signal; each row is one timestamp.

Caller's responsibility:
  - Build the panel by running each signal over the relevant time window
  - Drop NaN rows the substrate way ("real" missing data) vs zero rows
    ("signal explicitly didn't fire")
  - Pass it to orthogonalize_signals() and MaxIRCombiner

Substrate's responsibility:
  - Don't know what a "signal" is or how it's computed
  - Don't know about engine.signals.* concrete implementations
  - Provide the math for orthogonalization + max-IR combination
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class SignalScorePanel:
    """A time × signal panel of signal scores.

    Attributes:
      scores:        (T, K) — per-period scores. NaN where a signal wasn't
                     computable at that timestamp.
      signal_names:  K names aligned with `scores`' columns
      timestamps:    (T,) epoch-ms timestamps, monotone increasing

    The substrate doesn't impose a sign convention or scale on scores;
    each downstream consumer (orthogonalize, IC computation, combiner)
    handles whatever shape the caller produces. Convention is typically:
      - Per-period scores in [-1, +1] or unbounded
      - Sign matches the signal's directional prediction
      - Magnitude reflects confidence
    """

    scores: NDArray
    signal_names: list[str]
    timestamps: NDArray

    def __post_init__(self) -> None:
        if self.scores.ndim != 2:
            raise ValueError(f"scores must be 2D (T, K); got shape {self.scores.shape}")
        if self.scores.shape[1] != len(self.signal_names):
            raise ValueError(
                f"scores columns ({self.scores.shape[1]}) != "
                f"n_signal_names ({len(self.signal_names)})"
            )
        if self.scores.shape[0] != self.timestamps.size:
            raise ValueError(
                f"scores rows ({self.scores.shape[0]}) != "
                f"n_timestamps ({self.timestamps.size})"
            )

    @property
    def n_periods(self) -> int:
        return self.scores.shape[0]

    @property
    def n_signals(self) -> int:
        return self.scores.shape[1]

    def subset(self, signal_names: list[str]) -> "SignalScorePanel":
        """Return a new panel containing only the named signals (preserves order)."""
        keep = [self.signal_names.index(n) for n in signal_names if n in self.signal_names]
        return SignalScorePanel(
            scores=self.scores[:, keep],
            signal_names=[self.signal_names[i] for i in keep],
            timestamps=self.timestamps.copy(),
        )


@dataclass(frozen=True)
class InformationCoefficients:
    """Per-signal correlation with forward returns + summary stats.

    Attributes:
      values:        (K,) per-signal IC values
      signal_names:  K names aligned with `values`
      n_observations: number of (signal, forward_return) pairs used
      method:        "pearson" or "spearman" depending on caller's choice
    """

    values: NDArray
    signal_names: list[str] = field(default_factory=list)
    n_observations: int = 0
    method: str = "pearson"

    def to_dict(self) -> dict[str, float]:
        return {name: float(v) for name, v in zip(self.signal_names, self.values)}

    def top_n(self, n: int) -> list[tuple[str, float]]:
        """Top-N signals by absolute IC."""
        pairs = list(zip(self.signal_names, self.values))
        pairs.sort(key=lambda x: abs(x[1]), reverse=True)
        return [(name, float(v)) for name, v in pairs[:n]]


@dataclass(frozen=True)
class MaxIRWeights:
    """Result of fitting `MaxIRCombiner`.

    Attributes:
      weights:           (K,) signal weights — apply to per-period scores
      signal_names:      K names
      gross_leverage:    sum(|w_i|)
      net_leverage:      sum(w_i)
      in_sample_ic:      IC of the combined signal in-sample
      in_sample_ir:      Information ratio in-sample (annualized)
      n_observations:    rows used in the fit
      method:            "closed_form" / "mv_optimizer"
      converged:         did the fit succeed (mainly for the constrained path)
      shrinkage_lambda:  Ledoit-Wolf intensity used on the signal covariance
                         (NaN if shrinkage was disabled)
    """

    weights: NDArray
    signal_names: list[str] = field(default_factory=list)
    gross_leverage: float = 0.0
    net_leverage: float = 0.0
    in_sample_ic: float = float("nan")
    in_sample_ir: float = float("nan")
    n_observations: int = 0
    method: str = ""
    converged: bool = True
    shrinkage_lambda: float = float("nan")

    def to_dict(self) -> dict[str, float]:
        return {name: float(w) for name, w in zip(self.signal_names, self.weights)}

    def summary(self) -> str:
        lines = [
            f"MaxIRWeights  (n={self.n_observations}, method={self.method})",
            "─" * 56,
            f"  In-sample IC:   {self.in_sample_ic:>+8.4f}",
            f"  In-sample IR:   {self.in_sample_ir:>+8.4f}  (annualized)",
            f"  Gross leverage: {self.gross_leverage:>8.4f}",
            f"  Net leverage:   {self.net_leverage:>+8.4f}",
            f"  Shrinkage λ:    {self.shrinkage_lambda:>8.4f}",
            "",
            "  Weights:",
        ]
        for name, w in zip(self.signal_names, self.weights):
            lines.append(f"    {name:<24}  {w:>+8.4f}")
        return "\n".join(lines)
