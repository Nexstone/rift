"""Strategy capacity analysis — "how big can this strategy get?"

Every serious quant backtest must answer the capacity question: a strategy
that earns 50 bps per trade at $10k notional may earn -5 bps at $1M
notional, because impact eats the edge. Without capacity analysis, paper
backtests are misleading.

Three independent constraints — capacity is the *minimum* of all three:

  1. Impact constraint   — trade size where predicted impact ≤ a fraction
                           of the strategy's alpha-per-trade.
                           Default: impact ≤ alpha / 2 ("half-alpha" rule,
                           industry-standard sustainable execution.)

  2. ADV constraint      — trade size as a fixed percentage of average
                           daily $ volume. Default: 5% (prudent retail/MM
                           default; institutional desks often 10-20%).

  3. L2-depth constraint — what trade size fills within `max_slippage_bps`
                           of mid in the current observed L2 book. This is
                           the *instantaneous* liquidity constraint —
                           the trade you can do RIGHT NOW.

Two outputs:

  - `CapacityResult.max_trade_size_usd`   — single number, binding-constraint
                                            min of the three.
  - `CapacityResult.capacity_curve`       — AUM → expected net alpha bps after
                                            impact. Lets the user read off the
                                            max AUM for any alpha threshold.

Reference:
  Frazzini, A., Israel, R., Moskowitz, T. (2018). "Trading Costs of Asset
    Pricing Anomalies." NBER 23288. (Capacity quantification methodology.)
  Almgren, R. (2003). "Optimal execution with nonlinear impact functions
    and trading-enhanced risk." Applied Mathematical Finance 10, 1-18.
"""

from rift_substrate.capacity.core import (
    CapacityCurvePoint,
    CapacityResult,
    analyze_capacity,
    capacity_adv,
    capacity_impact,
    capacity_l2_depth,
)

__all__ = [
    "CapacityCurvePoint",
    "CapacityResult",
    "analyze_capacity",
    "capacity_adv",
    "capacity_impact",
    "capacity_l2_depth",
]
