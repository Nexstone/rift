"""RIFT Signal Factory — weak signals that combine into strong alpha.

Each signal is a simple function: coin + state → score (-1 to +1).
The aggregator combines all signals into ranked opportunities.

Architecture:
    Signal functions return a score and a human-readable reason.
    Positive = bullish. Negative = bearish. Zero = no opinion.
    Magnitude = confidence (0.1 = weak, 0.9 = strong).

Adding a new signal: write a function, register it with @signal decorator.
"""

from rift_engine.signals.base import Signal, SignalResult, signal, get_all_signals, compute_all_signals

# Import all signal modules to trigger @signal registration
import rift_engine.signals.funding          # noqa: F401
import rift_engine.signals.momentum         # noqa: F401
import rift_engine.signals.microstructure   # noqa: F401
import rift_engine.signals.volatility       # noqa: F401
import rift_engine.signals.cross_pair       # noqa: F401
import rift_engine.signals.seasonality      # noqa: F401
import rift_engine.signals.computed         # noqa: F401
import rift_engine.signals.hyperstats       # noqa: F401
import rift_engine.signals.realtime         # noqa: F401

from rift_engine.signals.aggregator import aggregate_signals, rank_opportunities
