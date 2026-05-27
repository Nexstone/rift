"""Event-driven backtest engine — tick replay + L2 walks + latency model.

When candle-level vectorized backtesting is too coarse — e.g., for market
making or any intra-bar-sensitive strategy — this module replays raw events
(ticks + L2 snapshots) through the strategy with proper execution simulation:

  - Latency: strategy sees an event at time t, its order arrives at t + Δ.
  - Slippage: marketable orders fill at L2-walk VWAP, not mid.
  - Partial fills: order size exceeds top-of-book → multi-level fills tracked.
  - Fee accrual: each fill stamped with substrate.frictions fee math.

This is the "real quant" backtest for HF / MM strategies. Daily / swing /
funding strategies typically don't need it — the existing candle-level
engine in `rift_engine` is sufficient. Ship event-driven for completeness
and for the cases where vectorized is wrong.

Reference:
  Almgren & Chriss (2001), Tóth et al. (2011) — execution + impact theory
  Hasbrouck (2007) "Empirical Market Microstructure" — event-driven models
"""

from rift_substrate.backtest.events import (
    BookEvent,
    Event,
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderType,
    TickEvent,
)
from rift_substrate.backtest.execution import (
    ExecutionSimulator,
)
from rift_substrate.backtest.runner import (
    BacktestContext,
    EventDrivenBacktestResult,
    EventStrategy,
    run_event_driven_backtest,
)

__all__ = [
    "BacktestContext",
    "BookEvent",
    "Event",
    "EventDrivenBacktestResult",
    "EventStrategy",
    "ExecutionSimulator",
    "FillEvent",
    "OrderEvent",
    "OrderSide",
    "OrderType",
    "TickEvent",
    "run_event_driven_backtest",
]
