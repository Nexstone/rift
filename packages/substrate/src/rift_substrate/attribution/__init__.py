"""PnL attribution — decompose returns and $ PnL into causal components.

A strategy's PnL has many drivers: market beta, style-factor exposures (size,
momentum, etc.), execution costs (fees, slippage, impact), funding income/cost,
and — what every quant actually wants to isolate — the *alpha*: the unexplained
return that survives all of the above.

This module provides two attribution views:

  attribute_returns(strategy_returns, factor_model)
      Time-series return decomposition. For each period, splits the strategy's
      return into alpha (the regression intercept) + factor contributions
      (loading × that factor's return) + residual. Useful for charts and for
      computing rolling-window alpha.

  attribute_pnl(returns, notional, factor_model, cost_breakdown)
      Dollar-denominated attribution. Same decomposition but multiplied by
      notional invested per period, plus a frictions cost rollup. The
      tearsheet line: "Of your $50k PnL, $40k was alpha, $8k was market beta,
      and $-3k was execution costs."

Both build on:
  - `rift_substrate.risk.FactorModel` — provides the regression (Phase 2a)
  - `rift_substrate.frictions` — provides the cost components (Phase 2b)

Statistical significance, R², and t-stats are inherited from the underlying
factor model's `decompose()`, which uses OLS+NW HAC by default.

Distinction from `rift_engine.attribution`:
  - engine.attribution does single-factor (market-only) regression on session logs.
    Simpler; consumed by reports.py and portfolio.py today.
  - substrate.attribution does multi-factor (MKT/SMB/UMD + extensions) with HAC SEs.
    Rigorous; the eventual replacement for engine.attribution.
"""

from rift_substrate.attribution.pnl import (
    PnLAttribution,
    attribute_pnl,
)
from rift_substrate.attribution.returns import (
    ReturnsAttribution,
    attribute_returns,
)

__all__ = [
    "PnLAttribution",
    "ReturnsAttribution",
    "attribute_pnl",
    "attribute_returns",
]
