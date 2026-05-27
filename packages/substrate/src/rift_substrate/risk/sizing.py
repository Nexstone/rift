"""Unified sizing entry point — composes vol-target / Kelly / limits / drawdown.

`size_position()` is the single function strategies call to decide how
much to deploy. It chains the substrate.risk primitives:

  1. Choose a base scaler via `method`:
        "vol_target"      — scale by realized-vol vs target
        "kelly"           — fractional Kelly from μ + σ²
        "fixed_fraction"  — constant fraction of capital
  2. Apply position limits (single-position cap, gross leverage)
  3. Apply drawdown scaling (if a controller is supplied)
  4. Return a SizingResult with the final $ size + full lineage

For multi-asset portfolio sizing (e.g., Phase 3 signal recombination
where you have N orthogonalized signals), call the optimizer directly
via `MeanVarianceOptimizer.optimize()` — this single-asset entry point
is for the common case of "I have one trade idea, how much do I bet?"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.drawdown import DrawdownController
from rift_substrate.risk.kelly import kelly_fraction_single
from rift_substrate.risk.limits import PositionLimits, apply_limits
from rift_substrate.risk.vol_target import vol_target_scaler


SizingMethod = Literal["vol_target", "kelly", "fixed_fraction"]


@dataclass(frozen=True)
class SizingResult:
    """Final sizing decision with lineage.

    Attributes:
      position_usd:           final $ size (signed: + long, − short, 0 flat)
      position_fraction:      final fraction of capital (signed)
      base_fraction:          fraction before drawdown scaling
      drawdown_scaler:        multiplier from drawdown controller (1.0 if no DD)
      limits_triggered:       names of constraints that fired
      method:                 which sizing method was used
      diagnostics:            method-specific extra info
    """

    position_usd: float
    position_fraction: float
    base_fraction: float
    drawdown_scaler: float = 1.0
    limits_triggered: list[str] = field(default_factory=list)
    method: str = ""
    diagnostics: dict = field(default_factory=dict)


def size_position(
    *,
    side: int,                              # +1 long, -1 short, 0 flat
    capital_usd: float,
    method: SizingMethod = "vol_target",
    # Vol-target params
    returns: NDArray | list[float] | None = None,
    target_vol_annualized: float = 0.15,
    periods_per_year: float = 365.0,
    vol_lookback_periods: int = 60,
    # Kelly params
    expected_return_per_period: float = 0.0,
    variance_per_period: float = 0.0,
    kelly_fraction: float = 0.5,
    # Fixed-fraction params
    fixed_fraction: float = 0.01,
    # Common
    limits: PositionLimits | None = None,
    drawdown_controller: DrawdownController | None = None,
    current_drawdown: float = 0.0,
    max_base_fraction: float = 1.0,
) -> SizingResult:
    """Decide position size for a single trade idea.

    Args:
      side:                          +1 / -1 / 0
      capital_usd:                   account equity to size against
      method:                        "vol_target" / "kelly" / "fixed_fraction"
      returns:                       (for vol_target) recent returns of the underlying
      target_vol_annualized:         (for vol_target) target annualized vol
      periods_per_year:              (for vol_target) annualization
      vol_lookback_periods:          (for vol_target) rolling window
      expected_return_per_period:    (for kelly) μ
      variance_per_period:           (for kelly) σ²
      kelly_fraction:                (for kelly) 0.5 = half-Kelly
      fixed_fraction:                (for fixed_fraction) constant fraction
      limits:                        PositionLimits to apply post-sizing
      drawdown_controller:           optional drawdown-based size scaling
      current_drawdown:              current strategy drawdown (positive fraction)
      max_base_fraction:             hard cap on |base_fraction| before limits

    Returns:
      `SizingResult` with final $ size + lineage.
    """
    if side not in (-1, 0, 1):
        raise ValueError(f"side must be -1, 0, or +1; got {side}")
    if capital_usd < 0:
        raise ValueError(f"capital_usd must be >= 0; got {capital_usd}")

    if side == 0 or capital_usd == 0:
        return SizingResult(
            position_usd=0.0,
            position_fraction=0.0,
            base_fraction=0.0,
            method=method,
        )

    # 1. Base fraction from chosen method
    diagnostics: dict = {}
    if method == "vol_target":
        if returns is None:
            raise ValueError("vol_target method requires `returns`")
        vt = vol_target_scaler(
            returns=returns,
            target_vol_annualized=target_vol_annualized,
            periods_per_year=periods_per_year,
            lookback_periods=vol_lookback_periods,
        )
        base_fraction = float(side * vt.scaler)
        diagnostics["realized_vol_annualized"] = vt.realized_vol_annualized
        diagnostics["vol_target_capped"] = vt.capped

    elif method == "kelly":
        f = kelly_fraction_single(
            expected_return_per_period=expected_return_per_period,
            variance_per_period=variance_per_period,
            fraction=kelly_fraction,
            max_fraction=max_base_fraction,
        )
        # Kelly's sign comes from μ; we override with `side`. If μ disagrees
        # with side, scale by 0 (don't trade against your own forecast).
        if side > 0 and f < 0:
            f = 0.0
        elif side < 0 and f > 0:
            f = 0.0
        base_fraction = abs(f) * side
        diagnostics["full_kelly_unscaled"] = expected_return_per_period / variance_per_period if variance_per_period > 0 else 0.0

    elif method == "fixed_fraction":
        base_fraction = float(side * abs(fixed_fraction))
        diagnostics["fixed_fraction"] = fixed_fraction

    else:
        raise ValueError(f"unknown method: {method!r}")

    # Clamp base fraction
    base_fraction = float(np.clip(base_fraction, -max_base_fraction, max_base_fraction))

    # 2. Apply limits (treat as single-asset by wrapping in 1-element vector)
    final_fraction = base_fraction
    triggered: list[str] = []
    if limits is not None:
        lim_result = apply_limits(
            proposed_weights=[base_fraction],
            limits=limits,
        )
        final_fraction = float(lim_result.weights[0])
        triggered = list(lim_result.triggered)

    # 3. Apply drawdown scaling
    dd_scaler = 1.0
    if drawdown_controller is not None:
        dd_scaler = float(drawdown_controller.size_scaler(current_drawdown))
        final_fraction = final_fraction * dd_scaler
        if dd_scaler < 1.0:
            triggered.append(f"drawdown_scaler={dd_scaler:.3f}")

    position_usd = final_fraction * capital_usd

    return SizingResult(
        position_usd=float(position_usd),
        position_fraction=float(final_fraction),
        base_fraction=float(base_fraction),
        drawdown_scaler=dd_scaler,
        limits_triggered=triggered,
        method=method,
        diagnostics=diagnostics,
    )
