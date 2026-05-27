"""Cross-impact analysis — basket execution costs with correlation effects.

Trading asset A moves not only A's price but also the prices of correlated
assets. For single-asset strategies this doesn't matter; for basket /
pairs / stat-arb strategies it does:

  - **Aligned basket** (long A + long B, ρ > 0): each leg adds to the other's
    impact → cross-impact ADDS cost.
  - **Hedged pair** (long A + short B, ρ > 0): each leg partially offsets
    the other's impact → cross-impact REDUCES cost. This is one reason
    pairs trades are more efficient than two separate one-sided trades.

Reuses the existing `ImpactModel` ABC (sqrt-law or empirical) for own-impact
magnitude. Cross-impact for asset i from trade in asset j is modeled as:

    Δ_i_from_j_bps = sign(q_j) × ρ_ij × own_impact_bps(j) × dampening_ij

where dampening_ij = 1 if i==j (full own-impact), else `cross_dampening`
(default 1.0; empirically a value < 1.0 may be more realistic since
cross-impact is typically weaker than own-impact even at full correlation).

Total impact on asset i = sum over all j of Δ_i_from_j_bps.
Cost from executing the basket = sum over i of q_i × impact_bps_i / 10000.

This is the linear-in-correlation approximation of the Bouchaud /
Mastromatteo / Tóth cross-impact framework — appropriate for RIFT's
quant-curious users (interpretable, composable with existing primitives)
and the rigor backbone for power users who want to fit their own ρ matrix
from historical fills.

Reference:
  Mastromatteo, I., Tóth, B., Bouchaud, J-P. (2017). "Cross-impact and the
    structure of order books." (Cross-impact framework + empirics.)
  Bouchaud, J-P., Bonart, J., Donier, J., Gould, M. (2018). "Trades, Quotes
    and Prices." Cambridge. Ch. 13 on multi-asset price impact.
"""

from rift_substrate.cross_impact.core import (
    BasketImpactResult,
    basket_impact,
    correlation_matrix,
)

__all__ = [
    "BasketImpactResult",
    "basket_impact",
    "correlation_matrix",
]
