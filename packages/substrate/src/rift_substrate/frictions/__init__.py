"""Frictions — execution cost models for backtest realism and pre-trade prediction.

The substrate frictions module covers SIX concerns a real quant expects:

  fees       — HL tiered fee schedule + builder fee composition
  funding    — funding accrual (long pays positive funding)
  markouts   — post-fill price moves at standard horizons (t+1s/10s/60s/300s)
  shortfall  — Implementation Shortfall (Perold 1988) decomposition
  slippage   — L2 order book walk → predicted fill VWAP + slippage bps
  impact     — sqrt-law baseline + empirical impact curve fitter
  cost       — TradeCost aggregator — one-shot pre-trade cost estimate

Two distinct use cases:

  PRE-TRADE prediction:  "How much will this trade cost?" → estimate_trade_cost()
  POST-TRADE attribution: "Why did this fill cost what it did?" → markouts +
                          Implementation Shortfall + actual fees

Distinction from `rift_engine.tca`:
  - `tca` analyzes observed session logs and emits grade-style reports.
  - `frictions` provides the primitives `tca` (or any caller) builds on.

References:
  Perold, A. F. (1988). "The Implementation Shortfall: Paper Versus Reality."
    Journal of Portfolio Management 14, 4-9.
  Almgren, R. & Chriss, N. (2001). "Optimal Execution of Portfolio Transactions."
    Journal of Risk 3, 5-39.
  Tóth, B. et al. (2011). "Anomalous price impact and the critical nature of
    liquidity in financial markets." Physical Review X 1, 021006.
  Frazzini, A., Israel, R., Moskowitz, T. (2018). "Trading Costs." NBER 23288.
"""

from rift_substrate.frictions.cost import TradeCost, estimate_trade_cost
from rift_substrate.frictions.fees import (
    FeeQuote,
    FeeSchedule,
    FeeTier,
    estimate_fee,
    load_default_schedule,
)
from rift_substrate.frictions.funding import (
    FundingAccrual,
    accrue_funding,
    expected_funding_cost,
)
from rift_substrate.frictions.impact import (
    EmpiricalImpactFitter,
    ImpactModel,
    SqrtLawImpact,
    sqrt_law_impact_bps,
)
from rift_substrate.frictions.markouts import (
    DEFAULT_HORIZONS_SECONDS,
    MarkoutSeries,
    compute_markouts,
)
from rift_substrate.frictions.shortfall import (
    Fill,
    ImplementationShortfall,
    implementation_shortfall,
)
from rift_substrate.frictions.slippage import (
    L2Level,
    L2WalkResult,
    walk_book,
)

__all__ = [
    "DEFAULT_HORIZONS_SECONDS",
    "EmpiricalImpactFitter",
    "FeeQuote",
    "FeeSchedule",
    "FeeTier",
    "Fill",
    "FundingAccrual",
    "ImpactModel",
    "ImplementationShortfall",
    "L2Level",
    "L2WalkResult",
    "MarkoutSeries",
    "SqrtLawImpact",
    "TradeCost",
    "accrue_funding",
    "compute_markouts",
    "estimate_fee",
    "estimate_trade_cost",
    "expected_funding_cost",
    "implementation_shortfall",
    "load_default_schedule",
    "sqrt_law_impact_bps",
    "walk_book",
]
