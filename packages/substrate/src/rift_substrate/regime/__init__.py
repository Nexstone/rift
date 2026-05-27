"""Regime detection primitives.

Two complementary tools:
  - `HMMRegimeDetector` — labels every observation with a learned regime
    state (e.g., bull / chop / bear via a Gaussian HMM).
  - `detect_changepoints` / `regime_segments` — identifies where the
    data-generating process shifts (structural breaks via PELT).

Strategy-agnostic. The user composes a detector into their strategy when
they want regime-aware behaviour. The engine has zero opinions about which
detector to use — that's a user choice expressed via workbench config or
direct Python composition.
"""

from rift_substrate.regime.changepoints import (
    ChangepointResult,
    detect_changepoints,
    regime_segments,
)
from rift_substrate.regime.hmm import HMMRegimeDetector

__all__ = [
    "ChangepointResult",
    "HMMRegimeDetector",
    "detect_changepoints",
    "regime_segments",
]
