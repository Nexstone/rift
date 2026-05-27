"""Promotion gate primitives + composer.

Each `gate_*` function returns a `GateResult` (named, with the metric value,
threshold, comparison, and pass/fail outcome). `evaluate_promotion()` runs
a configured subset and emits a `PromotionVerdict` aggregating them.

Each gate is independent — callers can use them à la carte, or wire them
together via `evaluate_promotion`. Thresholds are arguments, never global
constants — the module ships defaults but doesn't bake them in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rift_substrate.capacity import CapacityResult
from rift_substrate.stats.dsr import deflated_sharpe_ratio
from rift_substrate.stats.metrics import max_drawdown


# ─── GateResult / PromotionVerdict dataclasses ────────────────────────


@dataclass(frozen=True)
class GateResult:
    """Outcome of one promotion gate.

    Attributes:
      name:            short identifier ("deflated_sharpe", "capacity", ...)
      passed:          True if the gate cleared the threshold
      metric_value:    the actual value computed (e.g., DSR = 0.96)
      threshold:       the comparison threshold (e.g., 0.95)
      comparison:      ">=" or "<=" — direction of the gate
      details:         optional human-readable annotation
    """

    name: str
    passed: bool
    metric_value: float
    threshold: float
    comparison: str
    details: str = ""

    def line(self) -> str:
        mark = "✓" if self.passed else "✗"
        return (
            f"  {mark} {self.name:<20} "
            f"{self.metric_value:>10.4f}  {self.comparison} {self.threshold:<10.4f}  "
            f"{self.details}"
        ).rstrip()


@dataclass(frozen=True)
class PromotionVerdict:
    """Aggregate promotion outcome.

    `overall_passed` is True iff ALL gates pass — a single fail blocks
    promotion. This is the standard institutional discipline; relaxing it
    means writing a custom composer that's explicit about which gates can
    be optional.
    """

    overall_passed: bool
    gate_results: list[GateResult] = field(default_factory=list)

    def failures(self) -> list[GateResult]:
        return [g for g in self.gate_results if not g.passed]

    def summary(self) -> str:
        status = "PASS — promote" if self.overall_passed else "FAIL — do not promote"
        n_pass = sum(1 for g in self.gate_results if g.passed)
        lines = [
            f"PromotionVerdict  {status}",
            "─" * 64,
            f"  {n_pass}/{len(self.gate_results)} gates passed",
            "",
        ]
        for g in self.gate_results:
            lines.append(g.line())
        return "\n".join(lines)


# ─── Individual gates ────────────────────────────────────────────────


def gate_deflated_sharpe(
    observed_sharpe: float,
    n_observations: int,
    n_trials: int,
    variance_of_trial_sharpes: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    min_dsr: float = 0.95,
) -> GateResult:
    """DSR must exceed `min_dsr` (default 0.95 → 95% confidence the edge is real).

    Use n_trials=1 and variance=0 if no parameter sweep was performed
    (this reduces DSR to PSR with threshold=0).
    """
    dsr = deflated_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        n_observations=n_observations,
        n_trials=n_trials,
        variance_of_trial_sharpes=variance_of_trial_sharpes,
        skew=skew,
        kurtosis=kurtosis,
    )
    passed = np.isfinite(dsr) and dsr >= min_dsr
    return GateResult(
        name="deflated_sharpe",
        passed=bool(passed),
        metric_value=float(dsr if np.isfinite(dsr) else 0.0),
        threshold=float(min_dsr),
        comparison=">=",
        details=f"observed_sharpe={observed_sharpe:.3f}, n_trials={n_trials}",
    )


def gate_cv_pass_rate(
    fold_sharpes: NDArray | list[float],
    min_sharpe_per_fold: float = 0.5,
    min_pass_rate: float = 0.7,
) -> GateResult:
    """Fraction of CV folds whose OOS Sharpe ≥ `min_sharpe_per_fold` must be
    at least `min_pass_rate`.

    Catches strategies that learn the train fold but break out-of-sample.
    Use with `PurgedKFold` or `CombinatoriallyPurgedCV` fold-level results.
    """
    arr = np.asarray(fold_sharpes, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return GateResult(
            name="cv_pass_rate",
            passed=False,
            metric_value=0.0,
            threshold=float(min_pass_rate),
            comparison=">=",
            details="no valid fold Sharpes provided",
        )
    pass_rate = float(np.mean(arr >= min_sharpe_per_fold))
    passed = pass_rate >= min_pass_rate
    return GateResult(
        name="cv_pass_rate",
        passed=bool(passed),
        metric_value=pass_rate,
        threshold=float(min_pass_rate),
        comparison=">=",
        details=(
            f"{int(np.sum(arr >= min_sharpe_per_fold))}/{arr.size} folds ≥ "
            f"{min_sharpe_per_fold:.2f}"
        ),
    )


def gate_capacity(
    capacity_result: CapacityResult,
    min_trade_size_usd: float = 10_000.0,
) -> GateResult:
    """Max sustainable trade size must be at least `min_trade_size_usd`.

    A strategy whose half-alpha trade size is too small isn't worth the
    operational overhead. Default $10K is permissive — institutional shops
    set this much higher (e.g., $1M+).
    """
    metric = capacity_result.max_trade_size_usd
    passed = np.isfinite(metric) and metric >= min_trade_size_usd
    return GateResult(
        name="capacity",
        passed=bool(passed),
        metric_value=float(metric),
        threshold=float(min_trade_size_usd),
        comparison=">=",
        details=f"binding: {capacity_result.binding_constraint}",
    )


def gate_track_record(
    n_observations: int,
    n_trades: int | None = None,
    min_observations: int = 252,
    min_trades: int | None = 100,
) -> GateResult:
    """Minimum observations AND (if provided) minimum trade count.

    Defaults:
      - 252 observations = one trading year (daily)
      - 100 trades = enough to start trusting the Sharpe distribution

    For higher-frequency strategies, both thresholds need to scale up
    (the law of large numbers wants more samples).
    """
    obs_ok = n_observations >= min_observations
    if n_trades is not None and min_trades is not None:
        trades_ok = n_trades >= min_trades
        details = (
            f"obs={n_observations}/{min_observations}, "
            f"trades={n_trades}/{min_trades}"
        )
        passed = obs_ok and trades_ok
        # Report the more-binding metric
        if not obs_ok:
            metric = float(n_observations)
            thr = float(min_observations)
        elif not trades_ok:
            metric = float(n_trades)
            thr = float(min_trades)
        else:
            metric = float(n_observations)
            thr = float(min_observations)
    else:
        passed = obs_ok
        details = f"obs={n_observations}/{min_observations}"
        metric = float(n_observations)
        thr = float(min_observations)

    return GateResult(
        name="track_record",
        passed=bool(passed),
        metric_value=metric,
        threshold=thr,
        comparison=">=",
        details=details,
    )


def gate_max_drawdown(
    returns: NDArray | list[float],
    max_dd_pct: float = 0.20,
) -> GateResult:
    """Maximum drawdown must be no worse than `-max_dd_pct`.

    Default 0.20 means a drawdown deeper than -20% fails the gate.
    """
    arr = np.asarray(returns, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return GateResult(
            name="max_drawdown",
            passed=False,
            metric_value=0.0,
            threshold=float(-max_dd_pct),
            comparison=">=",
            details="< 2 valid return observations",
        )
    mdd = float(max_drawdown(arr))  # negative number
    passed = mdd >= -max_dd_pct
    return GateResult(
        name="max_drawdown",
        passed=bool(passed),
        metric_value=mdd,
        threshold=float(-max_dd_pct),
        comparison=">=",
        details=f"observed peak-to-trough {mdd:.2%}",
    )


# ─── Composer ────────────────────────────────────────────────────────


def evaluate_promotion(
    gates: list[GateResult],
) -> PromotionVerdict:
    """Aggregate a list of GateResults into a PromotionVerdict.

    Overall pass requires every gate to pass. Callers compose the gates
    they want — they're not all mandatory. Typical usage:

        verdict = evaluate_promotion([
            gate_deflated_sharpe(...),
            gate_cv_pass_rate(fold_sharpes),
            gate_capacity(cap_result),
            gate_track_record(n_obs, n_trades),
            gate_max_drawdown(returns),
        ])

        if verdict.overall_passed:
            promote_to_live(...)
        else:
            print(verdict.summary())  # see which gate(s) failed
    """
    overall = all(g.passed for g in gates) and len(gates) > 0
    return PromotionVerdict(overall_passed=overall, gate_results=list(gates))
