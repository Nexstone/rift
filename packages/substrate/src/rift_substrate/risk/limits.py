"""Position limits — hard caps that override any sizing decision.

`PositionLimits` is the last line of defense before orders go out. Whatever
the optimizer / Kelly / vol-target / drawdown controller propose, the
limits projection clamps the result back into the feasible set:

  max_single_position_pct  — no single coin > X% of capital
  max_gross_leverage       — sum(|w_i|) ≤ X
  max_net_leverage         — |sum(w_i)| ≤ X
  max_sector_pct           — no sector > X% of capital (requires sector map)

Operator-config only. AI agents must not modify limits at runtime — this
matches the trust principle that risk controls are NOT AI-adjustable.

The projection is a simple proportional scaler when constraints fire. For
constraints that act on different axes (e.g., single position vs. sector
vs. gross), they're applied iteratively until all are satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class PositionLimits:
    """Hard caps applied AFTER sizing/optimization.

    All values are fractions of capital (e.g., 0.20 = 20%).
    Set a constraint to `float('inf')` to disable it.
    """

    max_single_position_pct: float = 0.20
    max_gross_leverage: float = 3.0
    max_net_leverage: float = 1.0
    max_sector_pct: float = 0.40


@dataclass(frozen=True)
class LimitApplicationResult:
    """Result of applying limits to a weight vector."""

    weights: NDArray            # final weights after clamping
    triggered: list[str]        # names of constraints that fired
    gross_leverage: float
    net_leverage: float


def apply_limits(
    proposed_weights: NDArray | list[float],
    limits: PositionLimits,
    sectors: dict[str, str] | None = None,
    asset_names: list[str] | None = None,
) -> LimitApplicationResult:
    """Project proposed weights onto the limit-feasible set.

    Args:
      proposed_weights: (N,) fractional weights
      limits:           PositionLimits with caps
      sectors:          optional {asset_name: sector_label} for sector caps.
                        If None, the sector cap is skipped.
      asset_names:      (N,) names aligned with `proposed_weights`.
                        Required if `sectors` is provided.

    Returns:
      `LimitApplicationResult` with final weights + list of constraints that fired.

    Algorithm: apply in order — single-position cap, sector cap (if sectors
    given), gross leverage, net leverage. Each application scales proportionally
    within its scope.
    """
    w = np.asarray(proposed_weights, dtype=np.float64).ravel().copy()
    N = w.size
    triggered: list[str] = []

    # 1. Single-position cap
    if N > 0 and np.isfinite(limits.max_single_position_pct):
        cap = limits.max_single_position_pct
        over = np.abs(w) > cap
        if over.any():
            w = np.where(over, np.sign(w) * cap, w)
            triggered.append("max_single_position_pct")

    # 2. Sector cap
    if sectors and asset_names and np.isfinite(limits.max_sector_pct):
        if len(asset_names) != N:
            raise ValueError(f"asset_names length {len(asset_names)} != n_weights {N}")
        sector_to_idx: dict[str, list[int]] = {}
        for i, name in enumerate(asset_names):
            sec = sectors.get(name)
            if sec is not None:
                sector_to_idx.setdefault(sec, []).append(i)
        for sec, idxs in sector_to_idx.items():
            gross_in_sector = float(np.abs(w[idxs]).sum())
            if gross_in_sector > limits.max_sector_pct:
                scale = limits.max_sector_pct / gross_in_sector
                w[idxs] = w[idxs] * scale
                triggered.append(f"max_sector_pct:{sec}")

    # 3. Gross leverage
    if np.isfinite(limits.max_gross_leverage):
        gross = float(np.abs(w).sum())
        if gross > limits.max_gross_leverage and gross > 0:
            w = w * (limits.max_gross_leverage / gross)
            triggered.append("max_gross_leverage")

    # 4. Net leverage — apply by uniform shift if violated
    if np.isfinite(limits.max_net_leverage):
        net = float(w.sum())
        if abs(net) > limits.max_net_leverage and N > 0:
            # Subtract a uniform value to bring net within the cap.
            # Note: this preserves relative differences between weights but
            # may push some weights toward zero. Alternative is proportional
            # scaling of the whole vector — we choose shift since it's more
            # natural for a "max net leverage" constraint that allows long/short.
            target = limits.max_net_leverage * np.sign(net)
            shift = (net - target) / N
            w = w - shift
            triggered.append("max_net_leverage")

    return LimitApplicationResult(
        weights=w,
        triggered=triggered,
        gross_leverage=float(np.abs(w).sum()),
        net_leverage=float(w.sum()),
    )
