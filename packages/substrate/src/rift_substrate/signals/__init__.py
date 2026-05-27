"""Signal combination — orthogonalize + max-IR, the corrected direction.

The research finding (Kakushadze "How to Combine a Billion Alphas",
WorldQuant "101 Formulaic Alphas", Renaissance / AQR practitioner literature):

  Top quant shops absolutely DO combine many weak signals — but they
  DON'T do it via naive weight-averaging. They:

    1. Orthogonalize each signal against a factor model (so the "alpha"
       isn't really just market beta in disguise).
    2. Combine the orthogonalized residuals via a shrunk-covariance,
       max-IR optimizer (closed form when no constraints, MV optimizer
       when constraints exist).

This module provides the substrate primitives for both steps:

  orthogonalize_signals(panel, factor_returns)
      Strip factor exposure from each signal. Output: same panel shape,
      but each column is now factor-orthogonal residuals.

  MaxIRCombiner.fit(orthogonalized_panel, forward_returns).combine(scores)
      Compute optimal weights via closed-form (Σ^-1 · IC) with Ledoit-Wolf
      shrinkage, optionally constrained via the MV optimizer.

Composes Phase 2a (factor_model regression), Phase 2c (covariance + optimizer).

References:
  Kakushadze, Z. (2016). "How to Combine a Billion Alphas." arxiv:1603.05937.
  Kakushadze, Z. (2016). "101 Formulaic Alphas." arxiv:1601.00991.
  Grinold, R. C. (1989). "The Fundamental Law of Active Management." JPM 15(3).
  Clarke, R., De Silva, H., Thorley, S. (2002). "Portfolio Constraints and
    the Fundamental Law of Active Management." Financial Analysts Journal 58(5).
"""

from rift_substrate.signals.base import (
    InformationCoefficients,
    MaxIRWeights,
    SignalScorePanel,
)
from rift_substrate.signals.combine import (
    MaxIRCombiner,
    information_coefficients,
)
from rift_substrate.signals.microstructure import (
    book_imbalance,
    book_imbalance_zscore,
    spread_pressure,
    wall_intensity,
)
from rift_substrate.signals.orthogonalize import (
    OrthogonalizationResult,
    orthogonalize_signals,
)

__all__ = [
    "InformationCoefficients",
    "MaxIRCombiner",
    "MaxIRWeights",
    "OrthogonalizationResult",
    "SignalScorePanel",
    "book_imbalance",
    "book_imbalance_zscore",
    "information_coefficients",
    "orthogonalize_signals",
    "spread_pressure",
    "wall_intensity",
]
