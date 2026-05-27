"""Factor primitives — ReturnsPanel + Factor ABC.

A `Factor` consumes a `ReturnsPanel` and produces a (T,) array of factor
returns. Each factor enforces point-in-time discipline internally —
the value at index t is computed only from rows with index ≤ t.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class ReturnsPanel:
    """A time × asset panel of period returns.

    Attributes:
      returns:    (T, N) array of per-period returns. NaN where a coin
                  wasn't tradeable on that date — factors handle this gracefully.
      coins:      list of N coin names, aligned with `returns`' columns.
      timestamps: (T,) array of timestamps in epoch ms, monotone increasing.
      volumes:    (T, N) array of daily $ notional volumes. Required by SizeFactor.
                  If None, MarketFactor falls back to equal-weight.
    """

    returns: NDArray
    coins: list[str]
    timestamps: NDArray
    volumes: NDArray | None = None

    def __post_init__(self) -> None:
        # Sanity checks — fail loud at construction
        if self.returns.ndim != 2:
            raise ValueError(f"returns must be 2D; got shape {self.returns.shape}")
        if self.returns.shape[1] != len(self.coins):
            raise ValueError(
                f"returns columns ({self.returns.shape[1]}) != n_coins ({len(self.coins)})"
            )
        if self.returns.shape[0] != self.timestamps.size:
            raise ValueError(
                f"returns rows ({self.returns.shape[0]}) != n_timestamps ({self.timestamps.size})"
            )
        if self.volumes is not None and self.volumes.shape != self.returns.shape:
            raise ValueError(
                f"volumes shape {self.volumes.shape} != returns shape {self.returns.shape}"
            )

    @property
    def n_periods(self) -> int:
        return self.returns.shape[0]

    @property
    def n_coins(self) -> int:
        return self.returns.shape[1]


class Factor(ABC):
    """A factor producing a (T,) return series from a ReturnsPanel.

    Subclasses implement `build(panel) -> NDArray`. The output has the same
    length as `panel.timestamps` and is NaN at indices where the factor
    can't be computed (insufficient history, too few valid coins, etc.).

    Subclasses should document any panel fields they require beyond
    `returns` (e.g., `volumes` for SizeFactor).
    """

    name: str = "factor"

    @abstractmethod
    def build(self, panel: ReturnsPanel) -> NDArray:
        """Compute the factor return series for the panel's full time range."""
        raise NotImplementedError
