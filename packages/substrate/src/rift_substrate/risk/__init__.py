"""Risk primitives — factor models, covariance estimation, sizing, attribution.

Currently:
  factors/             — crypto factor library (MKT, SMB, UMD)
  factor_model         — FactorModel + DecompositionResult
  regression           — OLS+NW + Huber+NW (used internally by factor_model)
  covariance           — SampleCovariance + LedoitWolfCovariance (shrinkage)
  vol_target           — vol-targeted sizing
  kelly                — single + multi-asset Kelly sizing
  drawdown             — drawdown-triggered size reduction
  limits               — hard position constraints
  optimizer            — mean-variance with constraints (Markowitz)
  sizing               — unified entry point composing the above

Use cases:
  Pre-trade single-asset:  size_position(side, capital, method="vol_target", ...)
  Multi-asset portfolio:   MeanVarianceOptimizer().optimize(mu, Sigma, ...)
  Factor decomposition:    FactorModel.from_panel(...).decompose(strategy_returns)
"""

from rift_substrate.risk.covariance import (
    CovarianceEstimate,
    CovarianceEstimator,
    LedoitWolfCovariance,
    SampleCovariance,
)
from rift_substrate.risk.drawdown import (
    DrawdownController,
    DrawdownStep,
    default_schedule,
)
from rift_substrate.risk.factor_model import DecompositionResult, FactorModel
from rift_substrate.risk.factors import (
    Factor,
    MarketFactor,
    MomentumFactor,
    ReturnsPanel,
    SizeFactor,
)
from rift_substrate.risk.kelly import (
    KellyResult,
    kelly_fraction_single,
    kelly_weights_multi,
)
from rift_substrate.risk.limits import (
    LimitApplicationResult,
    PositionLimits,
    apply_limits,
)
from rift_substrate.risk.optimizer import (
    MeanVarianceOptimizer,
    OptimizationConstraints,
    OptimizationResult,
)
from rift_substrate.risk.regression import (
    RegressionResult,
    huber_regression,
    ols_with_newey_west,
)
from rift_substrate.risk.sizing import (
    SizingMethod,
    SizingResult,
    size_position,
)
from rift_substrate.risk.vol_target import (
    VolTargetResult,
    vol_target_position_usd,
    vol_target_scaler,
)

__all__ = [
    "CovarianceEstimate",
    "CovarianceEstimator",
    "DecompositionResult",
    "DrawdownController",
    "DrawdownStep",
    "Factor",
    "FactorModel",
    "KellyResult",
    "LedoitWolfCovariance",
    "LimitApplicationResult",
    "MarketFactor",
    "MeanVarianceOptimizer",
    "MomentumFactor",
    "OptimizationConstraints",
    "OptimizationResult",
    "PositionLimits",
    "RegressionResult",
    "ReturnsPanel",
    "SampleCovariance",
    "SizeFactor",
    "SizingMethod",
    "SizingResult",
    "VolTargetResult",
    "apply_limits",
    "default_schedule",
    "huber_regression",
    "kelly_fraction_single",
    "kelly_weights_multi",
    "ols_with_newey_west",
    "size_position",
    "vol_target_position_usd",
    "vol_target_scaler",
]
