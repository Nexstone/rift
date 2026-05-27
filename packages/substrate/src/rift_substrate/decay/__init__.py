"""Alpha decay analysis — how long does the signal stay alive?

A signal that predicts t+1 return doesn't necessarily predict t+10 return.
This module measures the Information Coefficient (IC) at each forward
horizon to surface the decay curve, then fits an exponential model to
extract a half-life — the single-number summary every quant uses:

  "My BTC funding signal has half-life of ~6 hours; my momentum signal
   has half-life of ~3 days."

The output guides three downstream decisions:

  1. **Holding period** — Grinold's Fundamental Law says optimal holding
     ≈ τ (the decay time constant). Holding longer dilutes IR; holding
     shorter pays unnecessary transaction costs.
  2. **Rebalance frequency** — should match the decay timescale.
  3. **Signal selection** — between two signals with the same IC at h=1,
     the slower-decaying one wins (more capacity to hold without IR loss).

Two complementary outputs:

  - **Full curve** (rigorous): IC at each horizon with bootstrap 95% CIs.
  - **Half-life summary** (accessible): one number τ × ln(2), in periods.

Frequency-agnostic: horizons are integer periods (not seconds/days). The
caller decides what one period means. 1-minute bars → horizons in minutes;
hourly bars → horizons in hours; etc.

Reference:
  Grinold, R. C. (1989). "The Fundamental Law of Active Management."
    Journal of Portfolio Management 15(3), 30-37.
  Grinold & Kahn (1999). "Active Portfolio Management." Ch. 6 on IR & IC.
  López de Prado (2018). "Advances in Financial Machine Learning." Ch. 8
    on the relationship between IC, breadth, and decay.
"""

from rift_substrate.decay.core import (
    AlphaDecayCurve,
    HalfLifeFit,
    compute_ic_curve,
    estimate_half_life,
    make_forward_returns,
)

__all__ = [
    "AlphaDecayCurve",
    "HalfLifeFit",
    "compute_ic_curve",
    "estimate_half_life",
    "make_forward_returns",
]
