"""Event types for the event-driven backtester.

All events are immutable, timestamp-bearing, ordered by `timestamp_ms`.
Subclassed via a sealed `Event` interface (just a marker for the runner's
type dispatch — Python doesn't enforce, but the runner asserts shapes).

Three event sources:
  - TickEvent: a trade print (price, size, side from the taker's perspective)
  - BookEvent: an L2 snapshot (bids descending, asks ascending)
  - FundingEvent: hourly funding rate update (optional — strategies that
                  care about funding accrual subscribe)

Two event sinks:
  - OrderEvent: from the strategy to the execution simulator
  - FillEvent:  from the execution simulator back to the strategy
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union

from rift_substrate.frictions.slippage import L2Level


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"      # marketable; fills at L2 walk
    LIMIT = "limit"        # passive; fills when book crosses limit_price


# ─── Input events (replayed from history) ─────────────────────────────


@dataclass(frozen=True)
class TickEvent:
    """A trade print observed at an instant in time.

    `side` is the AGGRESSOR side: "buy" means a taker bought (lifted the ask),
    "sell" means a taker sold (hit the bid). Aligns with HL S3 fill data.
    """

    timestamp_ms: int
    price: float
    size: float
    side: OrderSide


@dataclass(frozen=True)
class BookEvent:
    """An L2 order book snapshot.

    `bids` sorted DESCENDING by price (best bid first).
    `asks` sorted ASCENDING by price (best ask first).
    Top-of-book mid = (bids[0].price + asks[0].price) / 2 (when both exist).
    """

    timestamp_ms: int
    bids: list[L2Level] = field(default_factory=list)
    asks: list[L2Level] = field(default_factory=list)

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2.0

    @property
    def spread_bps(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.asks[0].price - self.bids[0].price) / mid * 10_000.0


# ─── Output events (strategy → simulator → strategy) ──────────────────


@dataclass(frozen=True)
class OrderEvent:
    """An order submitted by the strategy.

    Market orders fill at L2-walk VWAP off the most recent BookEvent.
    Limit orders rest in the simulated book until the market crosses
    them (a Tick on the opposite side at >= limit_price for sells, etc.).
    """

    timestamp_ms: int
    side: OrderSide
    size: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    order_id: str = ""


@dataclass(frozen=True)
class FillEvent:
    """A fill produced by the execution simulator.

    `slippage_bps` is computed vs. the mid at submit-time (when known)
    or vs. the fill itself when no book was available. Sign convention:
    positive = unfavorable (paid worse than mid).
    """

    timestamp_ms: int
    side: OrderSide
    fill_price: float
    fill_size: float
    fee_usd: float
    slippage_bps: float
    order_id: str = ""
    partial: bool = False


# Marker type for the runner — both input event classes look like this.
Event = Union[TickEvent, BookEvent]
