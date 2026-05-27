"""Vol-targeted sizing — scale positions to a target annualized volatility.

Vol targeting is AQR's signature approach: keep portfolio risk constant
across market regimes by scaling exposure inversely to realized volatility.
When markets are calm, position larger; when wild, smaller. The strategy's
risk profile stays consistent even as the underlying asset's vol changes.

Formula:
    scaler = target_vol_annualized / realized_vol_annualized

Where realized vol is computed from a rolling window of recent returns,
annualized using `periods_per_year_for_interval()`.

Two entry points:

  vol_target_scaler(returns, target_vol, ppy, lookback)
      Returns the dimensionless scaler in [0, ∞). Apply to your raw
      position sizing to get vol-targeted exposure.

  vol_target_position_usd(...)
      Convenience: returns $ position size given capital + signed direction.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class VolTargetResult:
    """Vol-targeted sizing decision with lineage."""

    scaler: float                       # the multiplier applied to base exposure
    realized_vol_annualized: float      # estimated current vol
    target_vol_annualized: float        # what we're targeting
    lookback_periods: int               # rolling window used
    capped: bool                        # whether scaler was clamped at max_scaler


def vol_target_scaler(
    returns: NDArray | list[float],
    target_vol_annualized: float,
    periods_per_year: float,
    lookback_periods: int = 60,
    max_scaler: float = 5.0,
    min_realized_vol: float = 1e-6,
) -> VolTargetResult:
    """Compute the position scaler to achieve `target_vol_annualized`.

    Args:
      returns:              (T,) per-period returns. Uses the LAST `lookback_periods`.
      target_vol_annualized: desired annualized vol (fractional, e.g., 0.15 = 15%)
      periods_per_year:     annualization factor (use periods_per_year_for_interval)
      lookback_periods:     rolling window for realized vol (default 60)
      max_scaler:           cap on the scaler to prevent runaway sizing under
                            crash-cliff volatility collapses (default 5.0)
      min_realized_vol:     floor on realized vol (avoid division by ~0)

    Returns:
      `VolTargetResult` with the scaler + lineage.
    """
    if target_vol_annualized <= 0:
        raise ValueError(f"target_vol_annualized must be > 0; got {target_vol_annualized}")
    if periods_per_year <= 0:
        raise ValueError(f"periods_per_year must be > 0; got {periods_per_year}")
    if lookback_periods < 2:
        raise ValueError(f"lookback_periods must be >= 2; got {lookback_periods}")

    r = np.asarray(returns, dtype=np.float64).ravel()
    r = r[np.isfinite(r)]
    if r.size == 0:
        # No data → can't size; return scaler=0 (no position)
        return VolTargetResult(
            scaler=0.0,
            realized_vol_annualized=float("nan"),
            target_vol_annualized=target_vol_annualized,
            lookback_periods=lookback_periods,
            capped=False,
        )

    window = r[-lookback_periods:] if r.size >= lookback_periods else r
    realized_per_period = float(np.std(window, ddof=1)) if window.size > 1 else 0.0
    realized_ann = realized_per_period * np.sqrt(periods_per_year)

    if realized_ann < min_realized_vol:
        # Vol collapsed to ~0 — clamp at max_scaler to prevent runaway.
        return VolTargetResult(
            scaler=max_scaler,
            realized_vol_annualized=realized_ann,
            target_vol_annualized=target_vol_annualized,
            lookback_periods=int(window.size),
            capped=True,
        )

    raw_scaler = target_vol_annualized / realized_ann
    capped = raw_scaler > max_scaler
    final_scaler = min(raw_scaler, max_scaler)

    return VolTargetResult(
        scaler=float(final_scaler),
        realized_vol_annualized=float(realized_ann),
        target_vol_annualized=float(target_vol_annualized),
        lookback_periods=int(window.size),
        capped=capped,
    )


def vol_target_position_usd(
    returns: NDArray | list[float],
    target_vol_annualized: float,
    periods_per_year: float,
    capital_usd: float,
    direction: int = 1,  # +1 long, -1 short, 0 flat
    lookback_periods: int = 60,
    max_scaler: float = 5.0,
) -> float:
    """Convenience wrapper: $ position size at vol-target exposure.

    Returns signed notional ($). The sign matches `direction`.
    """
    if direction == 0:
        return 0.0
    r = vol_target_scaler(
        returns=returns,
        target_vol_annualized=target_vol_annualized,
        periods_per_year=periods_per_year,
        lookback_periods=lookback_periods,
        max_scaler=max_scaler,
    )
    return float(direction * r.scaler * capital_usd)
