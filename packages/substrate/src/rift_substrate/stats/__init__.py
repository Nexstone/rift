"""Statistical machinery for RIFT — no data assumptions, pure math.

Every metric reported with confidence intervals. Every strategy comparison
adjusted for multiple testing. Every "best of N" result deflated for
selection bias.

Citations baked in:
  PSR — Bailey & López de Prado (2012)
  DSR — Bailey & López de Prado (2014)
  White's RC — White (2000)
  Stationary bootstrap — Politis & Romano (1994)
  Optimal block size — Politis & White (2004)
"""

from rift_substrate.stats.bootstrap import (
    optimal_block_size,
    stationary_bootstrap,
)
from rift_substrate.stats.dsr import deflated_sharpe_ratio
from rift_substrate.stats.metrics import (
    MetricBundle,
    Stats,
    periods_per_year_for_interval,
)
from rift_substrate.stats.multitest import (
    benjamini_hochberg,
    bonferroni,
    holm,
)
from rift_substrate.stats.psr import probabilistic_sharpe_ratio
from rift_substrate.stats.whites_rc import (
    RealityCheckResult,
    whites_reality_check,
)

__all__ = [
    "MetricBundle",
    "RealityCheckResult",
    "Stats",
    "benjamini_hochberg",
    "bonferroni",
    "deflated_sharpe_ratio",
    "holm",
    "optimal_block_size",
    "periods_per_year_for_interval",
    "probabilistic_sharpe_ratio",
    "stationary_bootstrap",
    "whites_reality_check",
]
