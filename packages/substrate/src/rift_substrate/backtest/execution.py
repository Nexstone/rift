"""Execution simulator — turns OrderEvents into FillEvents with realism.

Two execution modes:

  Market order:
    - Walks the last-known L2 book via substrate.frictions.walk_book.
    - Fills at VWAP across consumed levels; partial fill if book exhausted.
    - Slippage = walk_book.slippage_bps (already sign-adjusted).
    - Fee = fee_bps × notional, via substrate.frictions.estimate_fee.

  Limit order (passive):
    - Rests until a TickEvent crosses the limit price on the opposite side.
    - "Tick price ≤ limit_price for a buy" or "≥ for a sell" → fill at the limit.
    - Queue position is NOT modeled (would require seeing every cancel/replace);
      this is a first-MVP simplification. Real quants extend this.
    - No slippage (fills at limit by definition; "implementation shortfall"
      lives one layer up in `substrate.frictions.shortfall`).

Latency model:
  `latency_ms`: orders submitted at t arrive at t + latency_ms. The simulator
  doesn't see the order's effect until then. For RIFT's primary use cases
  (HL with multi-ms RTT) latency is small relative to bar size and rarely
  matters; for HFT-scale work, this needs extension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from rift_substrate.backtest.events import (
    BookEvent,
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderType,
    TickEvent,
)
from rift_substrate.frictions.fees import FeeSchedule, estimate_fee, load_default_schedule
from rift_substrate.frictions.slippage import walk_book


@dataclass
class _PendingLimit:
    """One resting limit order waiting for the market to cross."""

    order: OrderEvent
    arrival_ms: int  # when the order arrives at exchange (submit_ms + latency)


@dataclass
class ExecutionSimulator:
    """Stateful simulator: processes orders against the most recent book/tick.

    Attributes:
      latency_ms:        order submit-to-arrival latency in ms (default 50)
      fee_tier_volume_14d_usd: trader's 14d volume for fee tier lookup
      instrument:        "perp" or "spot" for fee schedule
      include_builder_fee: include RIFT builder fee in fee calc (default True)
      schedule:          override the default substrate fee schedule

    Internal state:
      current_book:      latest BookEvent seen
      pending_orders:    queue of submitted orders not yet arrived
      resting_limits:    queue of arrived limit orders not yet filled
    """

    latency_ms: int = 50
    fee_tier_volume_14d_usd: float = 0.0
    instrument: str = "perp"
    include_builder_fee: bool = True
    schedule: FeeSchedule | None = None

    current_book: BookEvent | None = field(default=None, init=False)
    pending_orders: list[_PendingLimit] = field(default_factory=list, init=False)
    resting_limits: list[_PendingLimit] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.schedule is None:
            try:
                self.schedule = load_default_schedule()
            except Exception:
                # Fall back to defaults inside estimate_fee
                self.schedule = None

    # ─── Public surface ───────────────────────────────────────────

    def drain_pending(self, up_to_ms: int) -> list[FillEvent]:
        """Process any pending orders that have matured by `up_to_ms`.

        Must be called BEFORE updating the book (so matured orders fill
        against the book in effect when they arrived, not the next snapshot).
        """
        fills: list[FillEvent] = []
        still_pending: list[_PendingLimit] = []
        for p in self.pending_orders:
            if p.arrival_ms <= up_to_ms:
                if p.order.order_type == OrderType.MARKET:
                    f = self._fill_market(p.order, p.arrival_ms)
                    if f is not None:
                        fills.append(f)
                else:
                    f = self._try_fill_limit_against_book(p.order, p.arrival_ms)
                    if f is not None:
                        fills.append(f)
                    else:
                        self.resting_limits.append(p)
            else:
                still_pending.append(p)
        self.pending_orders = still_pending
        return fills

    def update_book(self, book: BookEvent) -> None:
        """Record the latest L2 snapshot."""
        self.current_book = book

    def submit(self, order: OrderEvent) -> None:
        """Queue an order. It arrives at exchange after `latency_ms`."""
        arrival = order.timestamp_ms + self.latency_ms
        self.pending_orders.append(_PendingLimit(order=order, arrival_ms=arrival))

    def process_tick(self, tick: TickEvent) -> list[FillEvent]:
        """Advance time to `tick.timestamp_ms`, fill any orders that activate.

        Caller must drain pending market orders separately (via `drain_pending`)
        against the appropriate book. This method only handles limit-resting
        fills triggered by the tick itself.
        """
        fills: list[FillEvent] = []

        # Pending market orders → fill against current_book
        # (caller should have drained against the right book already, but
        #  defensively handle any stragglers that arrived between book updates)
        for p in list(self.pending_orders):
            if p.arrival_ms <= tick.timestamp_ms:
                if p.order.order_type == OrderType.MARKET:
                    f = self._fill_market(p.order, p.arrival_ms)
                    if f is not None:
                        fills.append(f)
                    self.pending_orders.remove(p)
                else:
                    f = self._try_fill_limit_against_book(p.order, p.arrival_ms)
                    if f is not None:
                        fills.append(f)
                        self.pending_orders.remove(p)
                    else:
                        self.resting_limits.append(p)
                        self.pending_orders.remove(p)

        # Resting limit orders: tick may cross them
        still_resting: list[_PendingLimit] = []
        for r in self.resting_limits:
            f = self._try_fill_limit_against_tick(r.order, tick)
            if f is not None:
                fills.append(f)
            else:
                still_resting.append(r)
        self.resting_limits = still_resting

        return fills

    # ─── Internal: fill logic ─────────────────────────────────────

    def _fill_market(self, order: OrderEvent, fill_ts: int) -> FillEvent | None:
        """Fill a market order at L2 walk price."""
        if self.current_book is None:
            return None
        book_side = self.current_book.asks if order.side == OrderSide.BUY else self.current_book.bids
        mid = self.current_book.mid
        if mid is None or not book_side:
            return None

        walk = walk_book(
            side="buy" if order.side == OrderSide.BUY else "sell",
            requested_size=order.size,
            book_side=book_side,
            mid_price=mid,
        )
        if walk.filled_size <= 0:
            return None

        notional = walk.filled_size * walk.fill_vwap
        fee_quote = estimate_fee(
            notional_usd=notional,
            is_taker=True,
            instrument=self.instrument,
            tier_volume_14d_usd=self.fee_tier_volume_14d_usd,
            include_builder_fee=self.include_builder_fee,
            schedule=self.schedule,
        )

        return FillEvent(
            timestamp_ms=fill_ts,
            side=order.side,
            fill_price=walk.fill_vwap,
            fill_size=walk.filled_size,
            fee_usd=fee_quote.total_usd,
            slippage_bps=walk.slippage_bps,
            order_id=order.order_id,
            partial=walk.unfilled_size > 0,
        )

    def _try_fill_limit_against_book(
        self, order: OrderEvent, fill_ts: int
    ) -> FillEvent | None:
        """Fill a limit order if the book is already through the limit price."""
        if self.current_book is None or order.limit_price is None:
            return None
        # Buy limit fills if best ask <= limit_price
        # Sell limit fills if best bid >= limit_price
        if order.side == OrderSide.BUY:
            if not self.current_book.asks or self.current_book.asks[0].price > order.limit_price:
                return None
        else:
            if not self.current_book.bids or self.current_book.bids[0].price < order.limit_price:
                return None

        return self._make_limit_fill(order, fill_ts, order.limit_price)

    def _try_fill_limit_against_tick(
        self, order: OrderEvent, tick: TickEvent
    ) -> FillEvent | None:
        """Resting limit: fill when a tick crosses the limit.

        For a buy limit at $100, a print at $99.50 from a seller proves the
        market reached the limit price → fill. For sells, symmetric.
        """
        if order.limit_price is None:
            return None
        if order.side == OrderSide.BUY and tick.price <= order.limit_price and tick.side == OrderSide.SELL:
            return self._make_limit_fill(order, tick.timestamp_ms, order.limit_price)
        if order.side == OrderSide.SELL and tick.price >= order.limit_price and tick.side == OrderSide.BUY:
            return self._make_limit_fill(order, tick.timestamp_ms, order.limit_price)
        return None

    def _make_limit_fill(
        self, order: OrderEvent, fill_ts: int, fill_price: float
    ) -> FillEvent:
        notional = order.size * fill_price
        fee_quote = estimate_fee(
            notional_usd=notional,
            is_taker=False,  # maker side for limit orders
            instrument=self.instrument,
            tier_volume_14d_usd=self.fee_tier_volume_14d_usd,
            include_builder_fee=self.include_builder_fee,
            schedule=self.schedule,
        )
        return FillEvent(
            timestamp_ms=fill_ts,
            side=order.side,
            fill_price=fill_price,
            fill_size=order.size,
            fee_usd=fee_quote.total_usd,
            slippage_bps=0.0,  # limit fills at limit by definition
            order_id=order.order_id,
            partial=False,
        )
