"""Time-series return attribution against a factor model.

`attribute_returns()` decomposes a strategy's returns into:

  alpha        — the regression intercept (the unexplained per-period return)
  factor_X     — loading_X × factor_X_return  (per period contribution)
  residual     — the regression residual (idiosyncratic noise)

The decomposition follows from time-series regression:
  r_t = α + Σ β_i · f_{i,t} + ε_t

where the factor returns f_i come from a fitted `FactorModel`. The output
preserves both the per-period series (for plotting, rolling stats, regime
analysis) and the cumulative sums (for "of your total return, X% was alpha").

Sign convention: positive means contributed positively to the strategy return.
Costs are NOT included here — see `attribute_pnl` for the $ version with cost
attribution rolled in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.factor_model import FactorModel


@dataclass(frozen=True)
class ReturnsAttribution:
    """Decomposition of a strategy's returns against a factor model.

    Per-period arrays (T,) and aggregate scalars. T is the number of periods
    where decomposition was possible (after NaN drops and factor warmup).

    Component contributions sum **arithmetically** to the total arithmetic
    return — that's the only way the decomposition is mathematically clean.
    The compound (geometric) total return is reported separately so users
    see the actual realized return; the gap between arithmetic and compound
    is volatility drag (proportional to half-variance), not a math error.

    Attributes:
      total_returns:      (T,) original strategy returns (NaN rows dropped)
      alpha_returns:      (T,) constant series at value `alpha_per_period`
      factor_returns:     dict[factor_name → (T,) loading × factor_t returns]
      residual_returns:   (T,) regression residuals

      total_return_arithmetic:   sum of per-period returns over the window
      total_return_compound:     compounded total: ∏(1+r) - 1 (the "real" return)
      alpha_arithmetic:          arithmetic contribution of alpha
      factor_arithmetic:         {factor_name → arithmetic contribution}
      residual_arithmetic:       arithmetic contribution of residuals
                                 (sum of these four equals total_return_arithmetic)

      alpha_per_period:   scalar α from the regression
      alpha_annualized:   α × periods_per_year
      alpha_tstat:        Newey-West-corrected t-stat
      alpha_pvalue:       two-sided p-value
      loadings:           dict of factor → β
      r_squared:          variance explained by factors
      n_observations:     T
      method:             regression method label (from underlying decompose)
    """

    total_returns: NDArray
    alpha_returns: NDArray
    factor_returns: dict[str, NDArray] = field(default_factory=dict)
    residual_returns: NDArray = field(default_factory=lambda: np.array([], dtype=np.float64))

    # Arithmetic decomposition — these sum cleanly to total_return_arithmetic
    total_return_arithmetic: float = 0.0
    alpha_arithmetic: float = 0.0
    factor_arithmetic: dict[str, float] = field(default_factory=dict)
    residual_arithmetic: float = 0.0

    # Compound total for reference (the actual realized return)
    total_return_compound: float = 0.0

    alpha_per_period: float = float("nan")
    alpha_annualized: float = float("nan")
    alpha_tstat: float = float("nan")
    alpha_pvalue: float = float("nan")
    loadings: dict[str, float] = field(default_factory=dict)
    r_squared: float = float("nan")
    n_observations: int = 0
    method: str = ""

    def summary(self) -> str:
        """Human-readable text summary."""
        lines = [
            f"ReturnsAttribution  (n={self.n_observations}, method={self.method})",
            "  Convention: components sum arithmetically to total_return_arithmetic.",
            "              total_return_compound is the realized compound return",
            "              (differs by vol drag = ~σ²/2).",
            "─" * 64,
            f"  Total return (compound):    {self.total_return_compound:>+8.2%}    ← actual realized",
            f"  Total return (arithmetic):  {self.total_return_arithmetic:>+8.2%}    ← sum of per-period returns",
            "",
            f"  Alpha (per-period):         {self.alpha_per_period:>+10.6f}"
            f"   t={self.alpha_tstat:+.2f}   p={self.alpha_pvalue:.4f}",
            f"  Alpha (annualized):         {self.alpha_annualized:>+8.2%}",
            f"  Alpha contribution:         {self.alpha_arithmetic:>+8.2%}",
            "",
            "  Factor contributions (arithmetic):",
        ]
        for name in self.loadings:
            beta = self.loadings.get(name, float("nan"))
            contrib = self.factor_arithmetic.get(name, float("nan"))
            lines.append(f"    {name:<6}  β={beta:>+7.4f}   contribution={contrib:>+8.2%}")
        lines.append("")
        lines.append(f"  Residual contribution:      {self.residual_arithmetic:>+8.2%}")
        lines.append(f"  R²:                         {self.r_squared:>+8.4f}")
        return "\n".join(lines)


def attribute_returns(
    strategy_returns: NDArray | list[float],
    factor_model: FactorModel,
    timestamps: NDArray | list[int] | None = None,
    use_robust: bool = False,
) -> ReturnsAttribution:
    """Decompose a strategy's returns against the factor model.

    Args:
      strategy_returns: (T,) per-period returns
      factor_model:     fitted `FactorModel` (from Phase 2a)
      timestamps:       optional (T,) epoch-ms timestamps for alignment
      use_robust:       use Huber+NW instead of OLS+NW for the regression
                        (recommended when returns are fat-tailed)

    Returns:
      `ReturnsAttribution` with per-period decomposition and cumulative aggregates.
    """
    s = np.asarray(strategy_returns, dtype=np.float64).ravel()
    # Pull the decomposition (alpha, loadings, residuals) from the factor model
    decomp = factor_model.decompose(
        strategy_returns=s,
        timestamps=timestamps,
        use_robust=use_robust,
    )

    if decomp.n_obs == 0:
        return ReturnsAttribution(
            total_returns=np.array([], dtype=np.float64),
            alpha_returns=np.array([], dtype=np.float64),
            method=decomp.method,
        )

    # Replay the regression: re-align Y and X exactly as decompose did
    Y, X = factor_model._align(s, timestamps)  # noqa: SLF001 — reusing internal alignment
    mask = np.isfinite(Y) & np.all(np.isfinite(X), axis=1)
    Y_clean = Y[mask]
    X_clean = X[mask]
    if Y_clean.size != decomp.n_obs:
        # Shouldn't happen, but be defensive
        return ReturnsAttribution(
            total_returns=Y_clean,
            alpha_returns=np.full(Y_clean.size, float("nan")),
            method=decomp.method,
            n_observations=Y_clean.size,
        )

    n = Y_clean.size

    # Per-period decomposition
    alpha_series = np.full(n, decomp.alpha)
    factor_contribs: dict[str, NDArray] = {}
    for i, name in enumerate(decomp.factor_names):
        beta = decomp.loadings.get(name, 0.0)
        factor_contribs[name] = beta * X_clean[:, i]

    residuals = decomp.residuals if decomp.residuals.size == n else (
        Y_clean - alpha_series - sum(factor_contribs.values())
    )

    def _arith(arr: NDArray) -> float:
        return float(np.sum(arr)) if arr.size else 0.0

    def _compound(arr: NDArray) -> float:
        return float(np.prod(1.0 + arr) - 1.0) if arr.size else 0.0

    return ReturnsAttribution(
        total_returns=Y_clean,
        alpha_returns=alpha_series,
        factor_returns=factor_contribs,
        residual_returns=residuals,
        # Arithmetic decomposition (sums cleanly)
        total_return_arithmetic=_arith(Y_clean),
        alpha_arithmetic=_arith(alpha_series),
        factor_arithmetic={name: _arith(arr) for name, arr in factor_contribs.items()},
        residual_arithmetic=_arith(residuals),
        # Compound total for reference
        total_return_compound=_compound(Y_clean),
        alpha_per_period=float(decomp.alpha),
        alpha_annualized=float(decomp.alpha_annualized),
        alpha_tstat=float(decomp.alpha_tstat),
        alpha_pvalue=float(decomp.alpha_pvalue),
        loadings=dict(decomp.loadings),
        r_squared=float(decomp.r_squared),
        n_observations=int(n),
        method=decomp.method,
    )
