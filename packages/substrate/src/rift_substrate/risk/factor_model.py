"""FactorModel — orchestration + decomposition.

A `FactorModel` holds a fitted set of factor return series and can decompose
any strategy returns series against them via time-series regression. This is
the substrate primitive that turns "rigorous alpha analysis" from theory into
a one-line call.

Two construction paths:

  FactorModel.from_panel(panel, factors=None, periods_per_year=365, seed=None)
      Primary entry — fit on an explicit ReturnsPanel. Used by tests, by
      power users who've built their own panel, and internally by `fit()`.

  FactorModel.fit(universe, lookback_months, interval, factors=None, seed=None)
      Convenience — loads the panel via substrate.Data for `universe` over
      `lookback_months` ending at "now", at `interval` resolution, then
      delegates to `from_panel`.

After fitting:

  model.factor_returns()                         # dict[name → (T,) NDArray]
  model.factor_returns_panel()                   # full (T, F) matrix
  model.decompose(strategy_returns, timestamps)  # → DecompositionResult

Time alignment is done by timestamp when `timestamps` is passed; otherwise
the strategy_returns are assumed to be already aligned to the factor panel.

Decomposition uses OLS+Newey-West by default; pass `use_robust=True` for
Huber+NW (recommended for fat-tailed strategies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from rift_substrate.risk.factors import (
    Factor,
    MarketFactor,
    MomentumFactor,
    ReturnsPanel,
    SizeFactor,
)
from rift_substrate.risk.regression import (
    huber_regression,
    ols_with_newey_west,
)


@dataclass(frozen=True)
class DecompositionResult:
    """Decomposition of a strategy's returns against a factor model.

    Coefficients are estimated via time-series regression with Newey-West
    HAC standard errors (OLS+NW by default; Huber+NW available).

    Fields:
      alpha:                    per-period intercept (the unexplained piece)
      alpha_annualized:         alpha × periods_per_year
      alpha_tstat:              t-statistic of intercept (HAC-corrected)
      alpha_pvalue:             two-sided p-value
      loadings:                 {factor_name: beta} — slope on each factor
      loading_tstats:           {factor_name: t-stat}
      loading_pvalues:          {factor_name: p-value}
      r_squared:                fraction of variance explained by factors
      residual_autocorr_lag1:   sanity check — large value means model misspecified
      residuals:                (n,) unexplained return series
      n_obs:                    observations used after dropping NaN
      method:                   "OLS+NW(L)" or "Huber+NW(L)"
      factor_names:             ordered list of factor names
    """

    alpha: float
    alpha_annualized: float
    alpha_tstat: float
    alpha_pvalue: float
    loadings: dict[str, float] = field(default_factory=dict)
    loading_tstats: dict[str, float] = field(default_factory=dict)
    loading_pvalues: dict[str, float] = field(default_factory=dict)
    r_squared: float = float("nan")
    residual_autocorr_lag1: float = float("nan")
    residuals: NDArray = field(default_factory=lambda: np.array([], dtype=np.float64))
    n_obs: int = 0
    method: str = ""
    factor_names: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary text. Same flavour as MetricBundle.summary()."""
        lines = [
            f"DecompositionResult  (n={self.n_obs}, method={self.method})",
            "─" * 64,
            f"  Alpha (per-period):   {self.alpha:>+10.6f}   t={self.alpha_tstat:+.2f}   p={self.alpha_pvalue:.4f}",
            f"  Alpha (annualized):   {self.alpha_annualized:>+10.4%}",
            "",
            "  Factor loadings:",
        ]
        for name in self.factor_names:
            b = self.loadings.get(name, float("nan"))
            t = self.loading_tstats.get(name, float("nan"))
            p = self.loading_pvalues.get(name, float("nan"))
            lines.append(f"    {name:<6} {b:>+8.4f}   t={t:+.2f}   p={p:.4f}")
        lines.append("")
        lines.append(f"  R²:                 {self.r_squared:>+8.4f}")
        lines.append(f"  Residual AC(1):     {self.residual_autocorr_lag1:>+8.4f}")
        return "\n".join(lines)


# ─── Internal state ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FittedState:
    """Internal state of a fitted FactorModel."""

    factor_returns: NDArray              # (T, F)
    factor_names: list[str]
    timestamps: NDArray                  # (T,) int64 epoch ms
    periods_per_year: float
    seed: int | None


# ─── FactorModel ──────────────────────────────────────────────────────


class FactorModel:
    """Fitted crypto factor model.

    Constructed via `from_panel()` or `fit()`. Once constructed, exposes
    factor_returns access + decompose() for any returns series.
    """

    def __init__(self, state: _FittedState):
        self._state = state

    # ─── Construction ─────────────────────────────────────────────────

    @classmethod
    def from_panel(
        cls,
        panel: ReturnsPanel,
        factors: Sequence[Factor] | None = None,
        periods_per_year: float = 365.0,
        seed: int | None = None,
    ) -> "FactorModel":
        """Fit on an explicit ReturnsPanel.

        Default factor set: [MarketFactor, SizeFactor, MomentumFactor] —
        the Liu-Tsyvinski-Wu (2022) 3-factor crypto model.

        Pass `factors=[...]` to substitute or extend.
        """
        if factors is None:
            factors = [MarketFactor(), SizeFactor(), MomentumFactor()]
        factor_returns = np.column_stack([f.build(panel) for f in factors])
        factor_names = [f.name for f in factors]
        state = _FittedState(
            factor_returns=factor_returns,
            factor_names=factor_names,
            timestamps=panel.timestamps.copy(),
            periods_per_year=float(periods_per_year),
            seed=seed,
        )
        return cls(state)

    # ─── Accessors ────────────────────────────────────────────────────

    def factor_returns(self) -> dict[str, NDArray]:
        """Dict of factor name → (T,) return series."""
        return {
            name: self._state.factor_returns[:, i].copy()
            for i, name in enumerate(self._state.factor_names)
        }

    def factor_returns_panel(self) -> NDArray:
        """Full (T, F) factor returns matrix. Read-only view (copy if mutating)."""
        return self._state.factor_returns.copy()

    @property
    def factor_names(self) -> list[str]:
        return list(self._state.factor_names)

    @property
    def timestamps(self) -> NDArray:
        return self._state.timestamps.copy()

    @property
    def periods_per_year(self) -> float:
        return self._state.periods_per_year

    # ─── Decompose ────────────────────────────────────────────────────

    def decompose(
        self,
        strategy_returns: NDArray | Sequence[float],
        timestamps: NDArray | Sequence[int] | None = None,
        use_robust: bool = False,
    ) -> DecompositionResult:
        """Decompose a strategy's returns against the fitted factors.

        Args:
          strategy_returns: (T,) per-period returns
          timestamps:       optional (T,) epoch-ms timestamps. If given,
                            inner-joins with the factor panel by timestamp.
                            If None, requires `strategy_returns` to be
                            aligned 1:1 with the factor panel.
          use_robust:       use Huber+NW (robust to outliers) instead of OLS+NW
        """
        Y, X = self._align(strategy_returns, timestamps)
        # Drop any rows with NaN
        mask = np.isfinite(Y) & np.all(np.isfinite(X), axis=1)
        Y_clean = Y[mask]
        X_clean = X[mask]

        if Y_clean.size < self._state.factor_returns.shape[1] + 5:
            return self._empty_result()

        regress = huber_regression if use_robust else ols_with_newey_west
        reg = regress(Y_clean, X_clean, add_constant=True)

        # Residual autocorrelation at lag 1 — sanity check on model adequacy
        residuals = reg.residuals
        autocorr = self._lag1_autocorr(residuals)

        loadings = {
            n: float(reg.coef[i + 1])
            for i, n in enumerate(self._state.factor_names)
        }
        loading_tstats = {
            n: float(reg.tstat[i + 1])
            for i, n in enumerate(self._state.factor_names)
        }
        loading_pvalues = {
            n: float(reg.pvalue[i + 1])
            for i, n in enumerate(self._state.factor_names)
        }

        return DecompositionResult(
            alpha=float(reg.coef[0]),
            alpha_annualized=float(reg.coef[0] * self._state.periods_per_year),
            alpha_tstat=float(reg.tstat[0]),
            alpha_pvalue=float(reg.pvalue[0]),
            loadings=loadings,
            loading_tstats=loading_tstats,
            loading_pvalues=loading_pvalues,
            r_squared=float(reg.r_squared) if np.isfinite(reg.r_squared) else 0.0,
            residual_autocorr_lag1=autocorr,
            residuals=residuals,
            n_obs=int(reg.n_obs),
            method=reg.method,
            factor_names=list(self._state.factor_names),
        )

    # ─── Internals ────────────────────────────────────────────────────

    def _align(
        self,
        strategy_returns: NDArray | Sequence[float],
        timestamps: NDArray | Sequence[int] | None,
    ) -> tuple[NDArray, NDArray]:
        """Return (Y, X) aligned to common timestamps."""
        Y_raw = np.asarray(strategy_returns, dtype=np.float64)
        F = self._state.factor_returns

        if timestamps is None:
            if Y_raw.size != F.shape[0]:
                raise ValueError(
                    f"strategy_returns length {Y_raw.size} != factor panel length {F.shape[0]}. "
                    "Pass timestamps to align by time."
                )
            return Y_raw, F

        ts = np.asarray(timestamps, dtype=np.int64)
        if ts.size != Y_raw.size:
            raise ValueError(
                f"timestamps length {ts.size} != strategy_returns length {Y_raw.size}"
            )
        common, ix_s, ix_f = np.intersect1d(
            ts, self._state.timestamps, return_indices=True
        )
        if common.size == 0:
            raise ValueError("no overlapping timestamps between strategy returns and factor panel")
        return Y_raw[ix_s], F[ix_f]

    @staticmethod
    def _lag1_autocorr(residuals: NDArray) -> float:
        if residuals.size < 2:
            return float("nan")
        centered = residuals - residuals.mean()
        var = float((centered ** 2).sum())
        if var <= 0:
            return float("nan")
        return float((centered[:-1] * centered[1:]).sum() / var)

    def _empty_result(self) -> DecompositionResult:
        names = list(self._state.factor_names)
        return DecompositionResult(
            alpha=float("nan"),
            alpha_annualized=float("nan"),
            alpha_tstat=float("nan"),
            alpha_pvalue=float("nan"),
            loadings={n: float("nan") for n in names},
            loading_tstats={n: float("nan") for n in names},
            loading_pvalues={n: float("nan") for n in names},
            r_squared=float("nan"),
            residual_autocorr_lag1=float("nan"),
            residuals=np.array([], dtype=np.float64),
            n_obs=0,
            method="insufficient_data",
            factor_names=names,
        )
