"""Cross-impact primitives.

Two functions:

  correlation_matrix(returns)
    Convenience: compute the (N, N) Pearson correlation matrix from a
    (T, N) returns panel. Handles NaN via pairwise complete observations.

  basket_impact(trades_usd, correlations, advs_usd, daily_vols, impact_model)
    The main primitive. Given a basket trade vector q (signed USD per asset)
    and the asset universe's correlation + liquidity profile, return per-asset
    impact (bps), per-asset cost (USD), the cross-impact-aware total cost,
    and a decomposition into diagonal (own-impact only) + cross terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rift_substrate.frictions.impact import ImpactModel


# ─── Dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BasketImpactResult:
    """Per-asset and total cross-impact accounting for one basket execution.

    Attributes:
      asset_names:        (N,) names aligned with trades
      trades_usd:         (N,) signed USD trades (+long, -short)
      impacts_bps:        (N,) total predicted impact on each asset's price,
                          aggregating own + all cross effects. Sign matches
                          the dominant direction (+ if price moves up).
      impact_costs_usd:   (N,) cost per asset = q_i × impact_bps_i / 10000.
                          Signed: positive = cost; negative = windfall
                          (cross-impact moved the price in your favor).
      total_cost_usd:     sum of impact_costs_usd
      diagonal_cost_usd:  cost if you ignored cross-impact entirely (just sum
                          of |q_i| × own_impact_bps_i / 10000)
      cross_term_usd:     total_cost_usd - diagonal_cost_usd. Positive →
                          cross-impact ADDS cost (aligned basket). Negative →
                          cross-impact REDUCES cost (hedged trade).
      cross_dampening:    dampening factor used (echo)
    """

    asset_names: list[str]
    trades_usd: NDArray
    impacts_bps: NDArray
    impact_costs_usd: NDArray
    total_cost_usd: float
    diagonal_cost_usd: float
    cross_term_usd: float
    cross_dampening: float

    def summary(self) -> str:
        def _fmt_usd(x: float) -> str:
            if not np.isfinite(x):
                return "    n/a "
            sign = "+" if x >= 0 else "-"
            absv = abs(x)
            if absv >= 1e6:
                return f"{sign}${absv / 1e6:>6.2f}M"
            if absv >= 1e3:
                return f"{sign}${absv / 1e3:>6.2f}K"
            return f"{sign}${absv:>7.2f}"

        lines = [
            f"BasketImpactResult  (cross_dampening={self.cross_dampening:.2f})",
            "─" * 60,
            f"  {'asset':<14} {'trade':>10}  {'impact':>9}  {'cost':>10}",
        ]
        for i, name in enumerate(self.asset_names):
            lines.append(
                f"  {name:<14} {_fmt_usd(self.trades_usd[i]):>10}  "
                f"{self.impacts_bps[i]:>+8.3f}b  {_fmt_usd(self.impact_costs_usd[i]):>10}"
            )
        lines.extend([
            "",
            f"  Total cost (cross-impact aware): {_fmt_usd(self.total_cost_usd)}",
            f"  Diagonal-only cost (baseline):   {_fmt_usd(self.diagonal_cost_usd)}",
            f"  Cross-term contribution:         {_fmt_usd(self.cross_term_usd)}",
        ])
        return "\n".join(lines)


# ─── Correlation helper ──────────────────────────────────────────────


def correlation_matrix(returns: NDArray) -> NDArray:
    """Pearson correlation matrix from a (T, N) returns panel.

    NaN-tolerant: uses pairwise complete observations. Output diagonal is
    exactly 1.0 by construction; matrix is forced symmetric (we don't trust
    floating point to do that for us).
    """
    R = np.asarray(returns, dtype=np.float64)
    if R.ndim != 2:
        raise ValueError(f"returns must be 2-D (T, N); got shape {R.shape}")
    T, N = R.shape
    if T < 2:
        raise ValueError(f"need at least 2 observations; got T={T}")

    rho = np.full((N, N), np.nan, dtype=np.float64)
    for i in range(N):
        rho[i, i] = 1.0
        for j in range(i + 1, N):
            mask = np.isfinite(R[:, i]) & np.isfinite(R[:, j])
            if mask.sum() < 2:
                continue
            xi = R[mask, i]
            xj = R[mask, j]
            if np.std(xi) == 0 or np.std(xj) == 0:
                rho[i, j] = 0.0
            else:
                rho[i, j] = float(np.corrcoef(xi, xj)[0, 1])
            rho[j, i] = rho[i, j]
    return rho


# ─── Basket impact ──────────────────────────────────────────────────


def basket_impact(
    trades_usd: NDArray | list[float],
    correlations: NDArray,
    advs_usd: NDArray | list[float],
    daily_vols: NDArray | list[float],
    impact_model: ImpactModel,
    cross_dampening: float = 1.0,
    asset_names: list[str] | None = None,
) -> BasketImpactResult:
    """Compute the cross-impact aware execution cost of a basket trade.

    For each ordered pair (i, j), the directional impact on asset i caused
    by the trade in asset j is:

        Δ_i_from_j_bps = sign(q_j) × ρ_ij × own_impact_bps(|q_j|, ADV_j, σ_j)
                       × (1.0 if i == j else cross_dampening)

    Total impact on i = Σ_j Δ_i_from_j_bps.
    Per-asset cost  = q_i × impact_bps_i / 10000  (signed).
    Total cost      = Σ_i per-asset cost.

    Args:
      trades_usd:        (N,) signed USD trade sizes
      correlations:      (N, N) correlation matrix; diagonal should be 1.0
      advs_usd:          (N,) average daily $ volume per asset
      daily_vols:        (N,) daily fractional volatility per asset
      impact_model:      ImpactModel (sqrt-law or empirical) — provides
                         own-impact magnitude
      cross_dampening:   scaling on off-diagonal terms (default 1.0 = full ρ).
                         Set to 0.0 to recover the naive sum-of-own-impacts.
                         Empirical work suggests <1.0 is more realistic
                         (cross-impact is typically weaker than own-impact
                         even at full correlation).
      asset_names:       optional names for the summary; default ["asset_0", ...]

    Returns:
      BasketImpactResult with per-asset and total accounting.

    Edge cases:
      - q_j = 0 contributes nothing (no own or cross effects from that asset)
      - non-finite ADV or vol propagates as NaN through that asset's contributions
      - asymmetric correlation matrix is accepted but the caller's
        responsibility; the matrix is used as-is
    """
    q = np.asarray(trades_usd, dtype=np.float64).ravel()
    rho = np.asarray(correlations, dtype=np.float64)
    adv = np.asarray(advs_usd, dtype=np.float64).ravel()
    sigma = np.asarray(daily_vols, dtype=np.float64).ravel()
    N = q.size

    if rho.shape != (N, N):
        raise ValueError(
            f"correlations must be ({N}, {N}); got {rho.shape}"
        )
    if adv.size != N or sigma.size != N:
        raise ValueError(
            f"advs_usd and daily_vols must each have length {N}; "
            f"got {adv.size} and {sigma.size}"
        )
    if not (0.0 <= cross_dampening <= 1.0):
        raise ValueError(
            f"cross_dampening must be in [0, 1]; got {cross_dampening}"
        )
    if asset_names is None:
        names = [f"asset_{i}" for i in range(N)]
    else:
        if len(asset_names) != N:
            raise ValueError(
                f"asset_names length {len(asset_names)} != N={N}"
            )
        names = list(asset_names)

    # Own-impact magnitude per trade (bps, non-negative)
    own_bps = np.zeros(N, dtype=np.float64)
    signs = np.sign(q)
    for j in range(N):
        if q[j] == 0:
            own_bps[j] = 0.0
        else:
            own_bps[j] = float(
                impact_model.predict_bps(float(abs(q[j])), float(adv[j]), float(sigma[j]))
            )

    # Build the directional contribution matrix C[i, j]:
    #   contribution to ΔP_i (bps) from trade j = sign(q_j) × ρ_ij × own_bps[j]
    #   × dampening (1 if i==j else cross_dampening)
    # Then impacts_bps_i = sum over j of C[i, j].
    dampening = np.full((N, N), cross_dampening, dtype=np.float64)
    np.fill_diagonal(dampening, 1.0)
    # C_ij = sign(q_j) * rho[i, j] * own_bps[j] * dampening[i, j]
    C = rho * (signs[np.newaxis, :] * own_bps[np.newaxis, :]) * dampening
    impacts_bps = C.sum(axis=1)

    # Per-asset cost = q_i × impact_bps_i / 10000  (signed)
    impact_costs_usd = q * impacts_bps / 10_000.0

    # Diagonal-only baseline: cost if cross effects were ignored entirely
    # = |q_i| × own_bps_i / 10000  (always non-negative; this is what you'd
    #   pay if every leg only affected its own price)
    diagonal_cost_usd = float(np.sum(np.abs(q) * own_bps / 10_000.0))
    total_cost_usd = float(np.sum(impact_costs_usd))
    cross_term_usd = total_cost_usd - diagonal_cost_usd

    return BasketImpactResult(
        asset_names=names,
        trades_usd=q,
        impacts_bps=impacts_bps,
        impact_costs_usd=impact_costs_usd,
        total_cost_usd=total_cost_usd,
        diagonal_cost_usd=diagonal_cost_usd,
        cross_term_usd=cross_term_usd,
        cross_dampening=float(cross_dampening),
    )
