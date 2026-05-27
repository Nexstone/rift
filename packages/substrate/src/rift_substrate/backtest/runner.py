"""Event-driven backtest runner — orchestrate replay + execution + accounting.

Inputs:
  - `EventStrategy` instance (the user's logic)
  - Iterable of events (TickEvent / BookEvent) in time order
  - Initial equity

Output: `EventDrivenBacktestResult` — trades, equity curve, PnL summary.

The runner is a tight loop:
  for event in events:
      if isinstance(event, BookEvent):
          exec_sim.update_book(event)
          orders = strategy.on_book(event, ctx)
      else:  # TickEvent
          fills = exec_sim.process_tick(event)
          for f in fills:
              apply fill to position; update equity; strategy.on_fill(f, ctx)
          orders = strategy.on_tick(event, ctx)
      for o in orders:
          exec_sim.submit(o)

No multi-asset support yet (one symbol per backtest). No funding-rate
accrual yet (FundingEvent type and handler would slot in similarly).
Both are forward-compat additions, not rewrites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from rift_substrate.backtest.events import (
    BookEvent,
    Event,
    FillEvent,
    OrderEvent,
    OrderSide,
    TickEvent,
)
from rift_substrate.backtest.execution import ExecutionSimulator


# ─── Strategy interface ───────────────────────────────────────────────


@dataclass
class BacktestContext:
    """Read-only view of state the strategy can use to make decisions.

    Updated by the runner before each strategy callback. Strategies should
    NOT mutate this — it's a snapshot, not a control surface.
    """

    timestamp_ms: int = 0
    position: float = 0.0          # signed size: +long, -short
    avg_entry_price: float = 0.0   # vwap of current position
    cash_usd: float = 0.0
    last_price: float = 0.0        # most recent tick price
    last_mid: float = 0.0          # most recent book mid (if any)
    n_fills: int = 0


class EventStrategy(ABC):
    """Strategy callback ABC.

    Implementations override `on_tick` (mandatory) and optionally `on_book`
    and `on_fill`. Callbacks may return a list of OrderEvents to submit,
    or `[]` for no action.
    """

    @abstractmethod
    def on_tick(
        self, event: TickEvent, context: BacktestContext
    ) -> list[OrderEvent]:
        """Called on every TickEvent. Return orders to submit (possibly empty)."""

    def on_book(
        self, event: BookEvent, context: BacktestContext
    ) -> list[OrderEvent]:
        """Optional — called on every BookEvent. Default no-op."""
        return []

    def on_fill(self, event: FillEvent, context: BacktestContext) -> None:
        """Optional — notified when one of your orders fills."""


# ─── Result ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventDrivenBacktestResult:
    """Output of `run_event_driven_backtest()`.

    Attributes:
      fills:           list of FillEvents in time order
      equity_curve:    list of equity values, one per fill event (post-fill)
      timestamps_ms:   timestamps aligned with equity_curve
      initial_equity:  starting capital
      final_equity:    ending equity
      total_pnl:       final - initial
      total_fees_usd:  sum of fees across all fills
      num_fills:       number of fills
      num_orders:      number of orders submitted
      final_position:  final signed position size
    """

    fills: list[FillEvent] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    timestamps_ms: list[int] = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_pnl: float = 0.0
    total_fees_usd: float = 0.0
    num_fills: int = 0
    num_orders: int = 0
    final_position: float = 0.0

    def summary(self) -> str:
        pnl_pct = (self.total_pnl / self.initial_equity * 100) if self.initial_equity > 0 else 0
        return "\n".join([
            f"EventDrivenBacktestResult",
            "─" * 56,
            f"  Initial equity:  ${self.initial_equity:,.2f}",
            f"  Final equity:    ${self.final_equity:,.2f}",
            f"  Total PnL:       ${self.total_pnl:+,.2f}  ({pnl_pct:+.2f}%)",
            f"  Total fees:      ${self.total_fees_usd:,.2f}",
            f"  Fills:           {self.num_fills}",
            f"  Orders:          {self.num_orders}",
            f"  Final position:  {self.final_position:+.6g}",
        ])


# ─── Runner ───────────────────────────────────────────────────────────


def run_event_driven_backtest(
    strategy: EventStrategy,
    events: Iterable[Event],
    initial_equity: float = 10_000.0,
    execution_simulator: ExecutionSimulator | None = None,
) -> EventDrivenBacktestResult:
    """Run the strategy over an event stream and return PnL accounting.

    Events MUST be in non-decreasing timestamp order. The runner does not
    re-sort them — callers are responsible for ordered input. This is
    intentional: pre-sorting is faster than checking each iteration.
    """
    exec_sim = execution_simulator or ExecutionSimulator()

    fills: list[FillEvent] = []
    equity_curve: list[float] = []
    timestamps_ms: list[int] = []

    position = 0.0          # signed
    avg_entry_price = 0.0   # vwap of current position
    cash = initial_equity
    total_fees = 0.0
    num_orders = 0

    last_price = 0.0
    last_mid = 0.0

    ctx = BacktestContext(
        timestamp_ms=0,
        position=0.0,
        avg_entry_price=0.0,
        cash_usd=initial_equity,
        last_price=0.0,
        last_mid=0.0,
        n_fills=0,
    )

    def _apply_fill(fill: FillEvent) -> None:
        """Cash + position bookkeeping.

        Cash moves with notional: buy spends cash, sell receives cash.
        Position changes with signed fill quantity.
        Fees always deducted from cash.
        avg_entry_price is tracked for the audit trail but PnL falls out
        of the standard `equity = cash + position * mark` identity, so we
        don't need explicit realized/unrealized splits here.
        """
        nonlocal position, avg_entry_price, cash, total_fees
        signed_qty = fill.fill_size if fill.side == OrderSide.BUY else -fill.fill_size
        notional = fill.fill_size * fill.fill_price

        # Cash flows: buys spend, sells receive
        if fill.side == OrderSide.BUY:
            cash -= notional
        else:
            cash += notional
        cash -= fill.fee_usd
        total_fees += fill.fee_usd

        # Update avg_entry_price for adding/reducing/flipping
        if position == 0 or (position > 0 and signed_qty > 0) or (position < 0 and signed_qty < 0):
            new_pos = position + signed_qty
            if new_pos != 0:
                avg_entry_price = (
                    (abs(position) * avg_entry_price + abs(signed_qty) * fill.fill_price)
                    / abs(new_pos)
                )
            position = new_pos
        else:
            reduce_qty = min(abs(signed_qty), abs(position))
            remaining_flip = abs(signed_qty) - reduce_qty
            if remaining_flip > 0:
                position = remaining_flip * (1 if signed_qty > 0 else -1)
                avg_entry_price = fill.fill_price
            else:
                position += signed_qty
                if position == 0:
                    avg_entry_price = 0.0

    def _record_fill(fill: FillEvent) -> None:
        """Apply a fill to position/cash, record in equity curve, notify strategy."""
        nonlocal last_price
        _apply_fill(fill)
        fills.append(fill)
        # Mark-to-fill: equity at the moment of fill
        mark_px = fill.fill_price
        equity = cash + position * mark_px
        equity_curve.append(equity)
        timestamps_ms.append(fill.timestamp_ms)
        # last_price is used as fallback mark — fill price is a real print
        if last_price <= 0:
            last_price = fill.fill_price
        ctx_after = BacktestContext(
            timestamp_ms=fill.timestamp_ms,
            position=position,
            avg_entry_price=avg_entry_price,
            cash_usd=cash,
            last_price=last_price,
            last_mid=last_mid,
            n_fills=len(fills),
        )
        strategy.on_fill(fill, ctx_after)

    for ev in events:
        ctx_ts = ev.timestamp_ms
        if isinstance(ev, BookEvent):
            # Drain any pending orders that matured before this book update — they
            # fill against the OLD book (the one in effect when they arrived).
            for f in exec_sim.drain_pending(ev.timestamp_ms):
                _record_fill(f)

            exec_sim.update_book(ev)
            mid = ev.mid
            if mid is not None:
                last_mid = mid
            # Strategy callback
            ctx = BacktestContext(
                timestamp_ms=ctx_ts,
                position=position,
                avg_entry_price=avg_entry_price,
                cash_usd=cash,
                last_price=last_price,
                last_mid=last_mid,
                n_fills=len(fills),
            )
            orders = strategy.on_book(ev, ctx) or []
            for o in orders:
                exec_sim.submit(o)
                num_orders += 1

        elif isinstance(ev, TickEvent):
            # Drain pending orders that matured before this tick — same logic
            # as books: they fill against the book that existed at arrival time.
            for f in exec_sim.drain_pending(ev.timestamp_ms):
                _record_fill(f)

            last_price = ev.price
            # Tick-triggered resting limit fills
            for f in exec_sim.process_tick(ev):
                _record_fill(f)

            ctx = BacktestContext(
                timestamp_ms=ctx_ts,
                position=position,
                avg_entry_price=avg_entry_price,
                cash_usd=cash,
                last_price=last_price,
                last_mid=last_mid,
                n_fills=len(fills),
            )
            orders = strategy.on_tick(ev, ctx) or []
            for o in orders:
                exec_sim.submit(o)
                num_orders += 1

    # Mark to last known price
    final_price = last_price if last_price > 0 else last_mid
    final_equity = cash + (position * final_price if final_price > 0 else 0.0)

    return EventDrivenBacktestResult(
        fills=fills,
        equity_curve=equity_curve,
        timestamps_ms=timestamps_ms,
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_pnl=final_equity - initial_equity,
        total_fees_usd=total_fees,
        num_fills=len(fills),
        num_orders=num_orders,
        final_position=position,
    )
