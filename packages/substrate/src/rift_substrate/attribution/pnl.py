"""Dollar-denominated PnL attribution.

Wraps `attribute_returns()` and multiplies through by notional invested per
period to produce $ contributions, then folds in a frictions cost breakdown
to give the canonical tearsheet line:

  Total PnL: $42,150
    Alpha contribution:        $38,200    (90.6%)
    Market (MKT) contribution: $ 6,300    (14.9%)
    Size (SMB):                $ 1,200    ( 2.8%)
    Momentum (UMD):            $   850    ( 2.0%)
    Costs:                     $-4,400   ( -10.4%)
      ├── Fees                 $-2,100
      ├── Funding              $-1,100
      ├── Slippage             $-1,000
      └── Impact               $-  200

The cost breakdown is supplied by the caller — usually computed by aggregating
per-trade `TradeCost` results from `substrate.frictions.estimate_trade_cost()`.
This module doesn't recompute costs; it just attributes already-known cost
totals against the strategy's gross PnL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rift_substrate.attribution.returns import ReturnsAttribution, attribute_returns
from rift_substrate.risk.factor_model import FactorModel


@dataclass(frozen=True)
class PnLAttribution:
    """Dollar-denominated decomposition of realized PnL.

    All `*_usd` fields are signed dollars. Sign convention:
      Positive contribution = added to PnL (good)
      Negative contribution = reduced PnL (cost, or adverse factor exposure)

    Attributes:
      total_pnl_usd:       sum of (return × notional) over the period, minus costs
      gross_pnl_usd:       same but excluding costs (the pre-friction PnL)

      alpha_pnl_usd:       contribution of the regression intercept
      factor_pnl_usd:      {factor_name: $ contribution}
      residual_pnl_usd:    unexplained portion

      cost_pnl_usd:        total cost (negative)
      cost_breakdown_usd:  {"fees": ..., "slippage": ..., "impact": ..., "funding": ...}

      alpha_pct:           alpha_pnl_usd / |total_pnl_usd|
      factor_pct:          {factor_name: $ contribution / |total_pnl_usd|}
      cost_pct:            cost_pnl_usd / |total_pnl_usd|

      n_periods:           number of attribution periods
      alpha_tstat:         from the underlying regression
      r_squared:           variance explained
      method:              regression method label
    """

    total_pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0

    alpha_pnl_usd: float = 0.0
    factor_pnl_usd: dict[str, float] = field(default_factory=dict)
    residual_pnl_usd: float = 0.0

    cost_pnl_usd: float = 0.0
    cost_breakdown_usd: dict[str, float] = field(default_factory=dict)

    alpha_pct: float = 0.0
    factor_pct: dict[str, float] = field(default_factory=dict)
    cost_pct: float = 0.0

    n_periods: int = 0
    alpha_tstat: float = float("nan")
    r_squared: float = float("nan")
    method: str = ""

    def summary(self) -> str:
        """Human-readable text summary with $ amounts and % contributions."""
        lines = [
            f"PnLAttribution  (n_periods={self.n_periods}, method={self.method})",
            "  Convention: positive = added to PnL; negative = reduced PnL.",
            "─" * 70,
            f"  Total PnL:                  ${self.total_pnl_usd:>+12,.2f}",
            f"    (gross before costs):     ${self.gross_pnl_usd:>+12,.2f}",
            "",
            f"  Alpha contribution:         ${self.alpha_pnl_usd:>+12,.2f}"
            f"   ({self.alpha_pct:>+6.1%})   t={self.alpha_tstat:+.2f}",
        ]
        for name in self.factor_pnl_usd:
            contrib = self.factor_pnl_usd[name]
            pct = self.factor_pct.get(name, 0.0)
            lines.append(
                f"  {name:<6} contribution:        ${contrib:>+12,.2f}   ({pct:>+6.1%})"
            )
        if self.residual_pnl_usd != 0:
            lines.append(
                f"  Residual (idio):            ${self.residual_pnl_usd:>+12,.2f}"
            )
        lines.append("")
        lines.append(
            f"  Costs total:                ${self.cost_pnl_usd:>+12,.2f}   ({self.cost_pct:>+6.1%})"
        )
        for name in self.cost_breakdown_usd:
            usd = self.cost_breakdown_usd[name]
            lines.append(f"    ├── {name:<20}    ${usd:>+12,.2f}")
        lines.append("")
        lines.append(f"  R²:                         {self.r_squared:.4f}")
        return "\n".join(lines)


def attribute_pnl(
    strategy_returns: NDArray | list[float],
    notional_usd: NDArray | list[float] | float,
    factor_model: FactorModel,
    cost_breakdown_usd: dict[str, float] | None = None,
    timestamps: NDArray | list[int] | None = None,
    use_robust: bool = False,
) -> PnLAttribution:
    """Decompose realized PnL into factor contributions + alpha + costs.

    Args:
      strategy_returns:    (T,) per-period returns
      notional_usd:        per-period notional invested, either:
                             - scalar: same notional for every period
                             - (T,) array: varying notional
      factor_model:        fitted `FactorModel`
      cost_breakdown_usd:  {"fees": $, "slippage": $, "funding": $, "impact": $}
                           Pass negative dollars for costs that reduced PnL.
                           If None, costs are 0.
      timestamps:          optional (T,) epoch-ms for timestamp-based alignment
      use_robust:          Huber+NW regression instead of OLS+NW

    Returns:
      `PnLAttribution` with $ decomposition and pct breakdown.
    """
    returns_attr = attribute_returns(
        strategy_returns=strategy_returns,
        factor_model=factor_model,
        timestamps=timestamps,
        use_robust=use_robust,
    )

    n = returns_attr.n_observations
    if n == 0:
        return PnLAttribution(method=returns_attr.method)

    # Notional vector
    if np.isscalar(notional_usd):
        notional_vec = np.full(n, float(notional_usd))
    else:
        notional_vec = np.asarray(notional_usd, dtype=np.float64).ravel()
        if notional_vec.size != n:
            # Allow caller to pass full original-length notional; trim to match.
            # If shorter, error out.
            if notional_vec.size < n:
                raise ValueError(
                    f"notional_usd has size {notional_vec.size} but attribution"
                    f" produced {n} observations"
                )
            notional_vec = notional_vec[-n:]

    # Per-period $ contributions
    alpha_usd = float(np.sum(returns_attr.alpha_returns * notional_vec))
    factor_usd = {
        name: float(np.sum(arr * notional_vec))
        for name, arr in returns_attr.factor_returns.items()
    }
    residual_usd = float(np.sum(returns_attr.residual_returns * notional_vec))
    gross_pnl = float(np.sum(returns_attr.total_returns * notional_vec))

    # Costs
    costs = dict(cost_breakdown_usd) if cost_breakdown_usd else {}
    cost_total = float(sum(costs.values())) if costs else 0.0

    total_pnl = gross_pnl + cost_total

    # Percentages — use abs(total_pnl) as denominator (sign-robust)
    denom = abs(total_pnl) if abs(total_pnl) > 1e-9 else 1.0
    alpha_pct = alpha_usd / denom
    factor_pct = {name: usd / denom for name, usd in factor_usd.items()}
    cost_pct = cost_total / denom

    return PnLAttribution(
        total_pnl_usd=total_pnl,
        gross_pnl_usd=gross_pnl,
        alpha_pnl_usd=alpha_usd,
        factor_pnl_usd=factor_usd,
        residual_pnl_usd=residual_usd,
        cost_pnl_usd=cost_total,
        cost_breakdown_usd=costs,
        alpha_pct=alpha_pct,
        factor_pct=factor_pct,
        cost_pct=cost_pct,
        n_periods=n,
        alpha_tstat=returns_attr.alpha_tstat,
        r_squared=returns_attr.r_squared,
        method=returns_attr.method,
    )
