"""Crypto factor library — MKT / SMB / UMD (Liu-Tsyvinski-Wu 2022).

Each Factor consumes a `ReturnsPanel` and produces a (T,) array of daily
factor returns. Point-in-time discipline is enforced inside each factor
so callers can't accidentally introduce look-ahead bias.
"""

from rift_substrate.risk.factors.base import Factor, ReturnsPanel
from rift_substrate.risk.factors.market import MarketFactor
from rift_substrate.risk.factors.momentum import MomentumFactor
from rift_substrate.risk.factors.size import SizeFactor

__all__ = [
    "Factor",
    "MarketFactor",
    "MomentumFactor",
    "ReturnsPanel",
    "SizeFactor",
]
