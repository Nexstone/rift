"""MKT — the crypto market factor.

Volume-weighted average of period returns across the universe (or equal-weight
if no volumes are provided). Captures broad market beta — the single biggest
factor in any equity or crypto return series.

Liu, Tsyvinski & Wu (2022) "Common Risk Factors in Cryptocurrency",
Journal of Finance — Market is one of their three canonical factors.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.factors.base import Factor, ReturnsPanel


class MarketFactor(Factor):
    """Vol-weighted crypto market index, period-by-period.

    For each period t, computes a weighted average of all coins' returns
    where the weights are the period's $ volumes. Falls back to equal-weight
    when `panel.volumes` is None.

    Coins with NaN returns at period t are dropped. Periods with fewer than
    `min_coins` valid coins return NaN — too few to call a "market" return.
    """

    name = "MKT"
    min_coins: int = 5

    def build(self, panel: ReturnsPanel) -> NDArray:
        T = panel.n_periods
        result = np.full(T, np.nan)
        for t in range(T):
            row = panel.returns[t]
            mask = np.isfinite(row)
            if mask.sum() < self.min_coins:
                continue
            valid_returns = row[mask]
            if panel.volumes is not None:
                vols = panel.volumes[t][mask]
                # Drop coins with NaN/zero volume from the weighted average
                vol_mask = np.isfinite(vols) & (vols > 0)
                if vol_mask.sum() < self.min_coins:
                    continue
                w = vols[vol_mask] / vols[vol_mask].sum()
                result[t] = float((valid_returns[vol_mask] * w).sum())
            else:
                result[t] = float(valid_returns.mean())
        return result
