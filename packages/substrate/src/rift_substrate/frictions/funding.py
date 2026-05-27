"""Funding accrual — pure math for HL's 1-hour perpetual funding cycle.

Sign convention (Hyperliquid + crypto industry standard):
  funding_rate > 0  →  longs PAY shorts (longs are paying to hold)
  funding_rate < 0  →  shorts PAY longs (shorts are paying to hold)

Therefore for a position holding cost (positive = cost to the holder):

  long_cost  = +size * funding_rate
  short_cost = -size * funding_rate

A long in a high-funding regime pays the rate each interval; a short in
the same regime earns it. Use `accrue_funding()` for backtest realization
(takes the actual per-interval rate series), or `expected_funding_cost()`
for pre-trade prediction (extrapolate current rate over a horizon).

HL pays funding every hour at the top of the hour. The `interval_hours`
parameter exists for forward-compat with venues whose funding interval differs,
but defaults to 1 to match HL.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class FundingAccrual:
    """Result of `accrue_funding()` — funding paid/received over a holding period.

    `total_paid_usd > 0` = position paid funding (cost). `< 0` = position earned funding.
    """

    total_paid_usd: float
    intervals_held: int
    average_rate: float          # mean of per-interval rates over the window
    cumulative_rate: float       # sum of per-interval rates (the "carry" over the period)


def accrue_funding(
    position_side: str,
    notional_usd: float,
    funding_rates: NDArray | list[float],
    interval_hours: float = 1.0,
) -> FundingAccrual:
    """Compute funding accrued over a holding period.

    Args:
      position_side: "long" or "short"
      notional_usd:  position notional in $ (price × size, signed positive)
      funding_rates: per-interval funding rates over the holding period.
                     Each rate is the FRACTIONAL rate for ONE interval
                     (NOT annualized). HL's hourly rates plug in directly.
      interval_hours: funding interval in hours (default 1.0 = HL).

    Returns:
      FundingAccrual with cumulative cost, intervals held, average + cumulative rate.

    Edge cases:
      - Empty rate series → zero-cost result
      - notional_usd of 0 → zero cost regardless of rates
      - NaN rates are dropped (skipped intervals)
    """
    if position_side not in ("long", "short"):
        raise ValueError(f"position_side must be 'long' or 'short'; got {position_side!r}")
    if notional_usd < 0:
        raise ValueError(f"notional_usd must be >= 0; got {notional_usd}")

    r = np.asarray(funding_rates, dtype=np.float64).ravel()
    r = r[np.isfinite(r)]
    n = r.size
    if n == 0:
        return FundingAccrual(0.0, 0, 0.0, 0.0)

    cumulative_rate = float(r.sum())
    sign = 1.0 if position_side == "long" else -1.0
    total_paid = sign * notional_usd * cumulative_rate

    return FundingAccrual(
        total_paid_usd=total_paid,
        intervals_held=int(n),
        average_rate=float(r.mean()),
        cumulative_rate=cumulative_rate,
    )


def expected_funding_cost(
    position_side: str,
    notional_usd: float,
    current_rate: float,
    holding_period_hours: float,
    *,
    interval_hours: float = 1.0,
    rate_drift_per_hour: float = 0.0,
) -> float:
    """Pre-trade estimate of funding cost over an expected holding period.

    Assumes the rate stays at `current_rate` (plus optional linear drift),
    integrated over `holding_period_hours / interval_hours` intervals.

    Args:
      position_side:        "long" or "short"
      notional_usd:         position notional in $
      current_rate:         current per-interval funding rate
      holding_period_hours: how long the position is expected to hold
      interval_hours:       funding interval (HL = 1 hour)
      rate_drift_per_hour:  optional linear drift in rate per hour
                            (e.g., expected mean reversion)

    Returns:
      Expected $ cost to the holder. Positive = cost; negative = income.
    """
    if holding_period_hours < 0:
        raise ValueError(f"holding_period_hours must be >= 0; got {holding_period_hours}")

    n_intervals = int(np.round(holding_period_hours / max(interval_hours, 1e-9)))
    if n_intervals <= 0:
        return 0.0

    if abs(rate_drift_per_hour) < 1e-12:
        # No drift — flat extrapolation
        cumulative = current_rate * n_intervals
    else:
        # Linear drift: rate at interval k = current + k * drift * interval_hours
        # Cumulative = n*current + drift*interval_hours * sum_{k=0..n-1} k
        #            = n*current + drift*interval_hours * n*(n-1)/2
        cumulative = (
            n_intervals * current_rate
            + rate_drift_per_hour * interval_hours * n_intervals * (n_intervals - 1) / 2.0
        )

    sign = 1.0 if position_side == "long" else -1.0
    return sign * notional_usd * cumulative
