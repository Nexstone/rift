"""SMB — size factor.

Long bottom-quantile by trailing 30-period average $ volume, short top-quantile.
"Small minus big" in equity factor literature; for crypto perps we proxy size
by 24h dollar volume since market cap of a perpetual contract isn't well-defined.

Refit cadence: ranks recompute every period (the trailing window slides).
Point-in-time: ranks at period t use volume from [t - lookback, t - 1].

Liu, Tsyvinski & Wu (2022) — Size is one of their three canonical factors.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.factors.base import Factor, ReturnsPanel


class SizeFactor(Factor):
    """Long-short factor: bottom-volume-quantile minus top-volume-quantile.

    Parameters:
      lookback_periods: trailing window for averaging volume (default 30)
      quantile:         fraction defining the buckets (default 0.3 → 30/30 split)
      min_valid:        minimum coins with valid data per period (default 10)
    """

    name = "SMB"

    def __init__(
        self,
        lookback_periods: int = 30,
        quantile: float = 0.3,
        min_valid: int = 10,
    ):
        if not 0 < quantile < 0.5:
            raise ValueError(f"quantile must be in (0, 0.5); got {quantile}")
        self.lookback_periods = lookback_periods
        self.quantile = quantile
        self.min_valid = min_valid

    def build(self, panel: ReturnsPanel) -> NDArray:
        if panel.volumes is None:
            raise ValueError("SizeFactor requires panel.volumes (got None)")
        T, N = panel.returns.shape
        result = np.full(T, np.nan)

        for t in range(self.lookback_periods, T):
            # Trailing avg volume per coin — point-in-time: [t-lookback, t-1]
            window_vol = panel.volumes[t - self.lookback_periods : t]
            with np.errstate(invalid="ignore"):
                avg_vol = np.nanmean(window_vol, axis=0)

            row = panel.returns[t]
            valid = np.isfinite(avg_vol) & np.isfinite(row) & (avg_vol > 0)
            if valid.sum() < self.min_valid:
                continue

            valid_vols = avg_vol[valid]
            valid_rets = row[valid]

            low_cutoff = np.quantile(valid_vols, self.quantile)
            high_cutoff = np.quantile(valid_vols, 1.0 - self.quantile)
            long_mask = valid_vols <= low_cutoff   # bottom = small = LONG
            short_mask = valid_vols >= high_cutoff  # top = big = SHORT

            if long_mask.sum() == 0 or short_mask.sum() == 0:
                continue

            long_ret = float(valid_rets[long_mask].mean())
            short_ret = float(valid_rets[short_mask].mean())
            result[t] = long_ret - short_ret

        return result
