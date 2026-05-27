"""UMD — momentum factor.

Long top-quintile by past compounded return, short bottom-quintile. Skips
the most recent `skip_periods` periods to avoid microstructure / reversal
noise (a common pattern in equity momentum: rank on [t-12mo, t-1mo],
realize [t, t+1]).

Refit cadence: ranks recompute every period using a sliding window.
Point-in-time: ranks at period t use returns from [t - skip - lookback, t - skip - 1].

Liu, Tsyvinski & Wu (2022) — Momentum is one of their three canonical factors.
The skip is standard practice (Jegadeesh & Titman 1993 original, plus crypto
adaptations in LTW and later work).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.factors.base import Factor, ReturnsPanel


class MomentumFactor(Factor):
    """Long-short factor: top-momentum minus bottom-momentum.

    Parameters:
      lookback_periods: trailing window for past-return ranking (default 30)
      skip_periods:     periods to skip before lookback (default 7; avoids
                        microstructure noise / mean-reversal)
      quantile:         fraction defining buckets (default 0.2 → top/bottom 20%)
      min_valid:        minimum coins with valid data per period (default 10)
    """

    name = "UMD"

    def __init__(
        self,
        lookback_periods: int = 30,
        skip_periods: int = 7,
        quantile: float = 0.2,
        min_valid: int = 10,
    ):
        if not 0 < quantile < 0.5:
            raise ValueError(f"quantile must be in (0, 0.5); got {quantile}")
        if skip_periods < 0:
            raise ValueError(f"skip_periods must be >= 0; got {skip_periods}")
        self.lookback_periods = lookback_periods
        self.skip_periods = skip_periods
        self.quantile = quantile
        self.min_valid = min_valid

    def build(self, panel: ReturnsPanel) -> NDArray:
        T, N = panel.returns.shape
        result = np.full(T, np.nan)
        min_t = self.lookback_periods + self.skip_periods
        if min_t >= T:
            return result

        for t in range(min_t, T):
            # Past compounded return per coin, point-in-time
            #   window = returns[t - skip - lookback : t - skip]
            start = t - self.skip_periods - self.lookback_periods
            end = t - self.skip_periods
            window = panel.returns[start:end]

            # Require all periods in the window to be valid for each coin
            all_valid_mask = np.all(np.isfinite(window), axis=0)
            past_returns = np.full(N, np.nan)
            if all_valid_mask.any():
                past_returns[all_valid_mask] = (
                    np.prod(1.0 + window[:, all_valid_mask], axis=0) - 1.0
                )

            row = panel.returns[t]
            valid = np.isfinite(past_returns) & np.isfinite(row)
            if valid.sum() < self.min_valid:
                continue

            past = past_returns[valid]
            rets = row[valid]

            high_cutoff = np.quantile(past, 1.0 - self.quantile)
            low_cutoff = np.quantile(past, self.quantile)
            long_mask = past >= high_cutoff
            short_mask = past <= low_cutoff

            if long_mask.sum() == 0 or short_mask.sum() == 0:
                continue

            long_ret = float(rets[long_mask].mean())
            short_ret = float(rets[short_mask].mean())
            result[t] = long_ret - short_ret

        return result
