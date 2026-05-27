"""Tests for substrate.backtest — event-driven engine.

Pins behavior on:
  1. Cash accounting: notional + fees both flow through cash correctly.
  2. Position lifecycle: open, add, reduce, flip, close.
  3. Latency: orders don't fill before arrival_ms.
  4. L2 walk: market orders consume multiple levels for large size.
  5. Partial fills: requested > available → unfilled remainder.
  6. Limit orders: rest until book crosses, fill at limit price.
  7. Book-at-arrival: pending orders see the book in effect at maturity.
"""

from __future__ import annotations

import pytest

from rift_substrate.backtest import (
    BookEvent,
    EventStrategy,
    ExecutionSimulator,
    FillEvent,
    OrderEvent,
    OrderSide,
    OrderType,
    TickEvent,
    run_event_driven_backtest,
)
from rift_substrate.frictions.slippage import L2Level


# ─── Helpers ─────────────────────────────────────────────────────────


def _book(ts: int, bid: float, ask: float, depth: float = 100.0) -> BookEvent:
    return BookEvent(
        timestamp_ms=ts,
        bids=[L2Level(bid, depth)],
        asks=[L2Level(ask, depth)],
    )


def _deep_book(
    ts: int, bid_levels: list[tuple[float, float]], ask_levels: list[tuple[float, float]]
) -> BookEvent:
    return BookEvent(
        timestamp_ms=ts,
        bids=[L2Level(p, s) for p, s in bid_levels],
        asks=[L2Level(p, s) for p, s in ask_levels],
    )


class _OneShotBuy(EventStrategy):
    """Submit a single BUY on the first tick, then idle."""

    def __init__(self, size: float = 1.0, order_type=OrderType.MARKET, limit_price=None):
        self.fired = False
        self.size = size
        self.order_type = order_type
        self.limit_price = limit_price

    def on_tick(self, event, context):
        if not self.fired:
            self.fired = True
            return [OrderEvent(
                timestamp_ms=event.timestamp_ms,
                side=OrderSide.BUY,
                size=self.size,
                order_type=self.order_type,
                limit_price=self.limit_price,
            )]
        return []


class _RoundTrip(EventStrategy):
    """Buy on first tick, sell on second tick."""

    def __init__(self, size: float = 1.0):
        self.bought = False
        self.sold = False
        self.size = size

    def on_tick(self, event, context):
        if not self.bought:
            self.bought = True
            return [OrderEvent(timestamp_ms=event.timestamp_ms, side=OrderSide.BUY, size=self.size)]
        if not self.sold and context.position > 0:
            self.sold = True
            return [OrderEvent(timestamp_ms=event.timestamp_ms, side=OrderSide.SELL, size=self.size)]
        return []


# ─── BookEvent properties ────────────────────────────────────────────


class TestBookEvent:
    def test_mid_computed_from_top_levels(self):
        b = _book(0, 99.0, 101.0)
        assert b.mid == pytest.approx(100.0)

    def test_mid_none_if_empty_side(self):
        b = BookEvent(timestamp_ms=0, bids=[], asks=[L2Level(100, 5)])
        assert b.mid is None

    def test_spread_bps(self):
        b = _book(0, 99.95, 100.05)
        # spread = 0.10, mid = 100 → 10 bps
        assert b.spread_bps == pytest.approx(10.0)


# ─── Cash + position accounting ──────────────────────────────────────


class TestCashAccounting:
    def test_buy_only_pnl(self):
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 110.0, 1.0, OrderSide.BUY),  # price rallies
        ]
        r = run_event_driven_backtest(_OneShotBuy(size=1.0), events, 10_000.0)
        # Buy 1 @ $100.5, mark at $110, fee ~$0.075
        # PnL = (110 - 100.5) - 0.075 ≈ $9.42
        assert 9.0 < r.total_pnl < 10.0
        assert r.final_position == pytest.approx(1.0)
        assert r.num_fills == 1

    def test_round_trip_realizes_gain(self):
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            _book(500, 104.5, 105.5),
            TickEvent(1000, 105.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 105.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(_RoundTrip(size=1.0), events, 10_000.0)
        # BUY @ 100.5 (vs t=0 book), SELL @ 104.5 (vs t=500 book)
        # PnL = 4.0 - 2 fees (~0.15) = ~3.85
        assert 3.5 < r.total_pnl < 4.0
        assert abs(r.final_position) < 1e-9
        assert r.num_fills == 2

    def test_round_trip_realizes_loss(self):
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            _book(500, 94.5, 95.5),
            TickEvent(1000, 95.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 95.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(_RoundTrip(size=1.0), events, 10_000.0)
        # BUY @ 100.5, SELL @ 94.5 → loss of $6 + fees
        assert -7.0 < r.total_pnl < -5.5
        assert abs(r.final_position) < 1e-9

    def test_short_then_cover_realizes_gain_when_price_falls(self):
        class _Short(EventStrategy):
            def __init__(self):
                self.sold = False
                self.covered = False
            def on_tick(self, event, context):
                if not self.sold:
                    self.sold = True
                    return [OrderEvent(event.timestamp_ms, OrderSide.SELL, 1.0)]
                if not self.covered and context.position < 0:
                    self.covered = True
                    return [OrderEvent(event.timestamp_ms, OrderSide.BUY, 1.0)]
                return []

        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            _book(500, 94.5, 95.5),
            TickEvent(1000, 95.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 95.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(_Short(), events, 10_000.0)
        # SHORT @ 99.5 (bid), COVER @ 95.5 (ask) → gain ~$4 - fees
        assert 3.5 < r.total_pnl < 4.0
        assert abs(r.final_position) < 1e-9


# ─── Latency ─────────────────────────────────────────────────────────


class TestLatency:
    def test_order_does_not_fill_before_arrival(self):
        """Order submitted at t=100 with 500ms latency must not fill at t=200."""
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            # tick at t=200, before arrival (100 + 500 = 600)
            TickEvent(200, 100.0, 0.1, OrderSide.BUY),
            # tick at t=700, after arrival → fill
            TickEvent(700, 100.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(
            _OneShotBuy(size=1.0), events, 10_000.0,
            execution_simulator=ExecutionSimulator(latency_ms=500),
        )
        # Should fill exactly once, at the tick after arrival
        assert r.num_fills == 1
        # Fill should be timestamped at arrival_ms (100 + 500 = 600), not the tick
        assert r.fills[0].timestamp_ms == 600


# ─── L2 walk for large market orders ─────────────────────────────────


class TestL2Walk:
    def test_large_order_walks_multiple_levels(self):
        events = [
            _deep_book(
                0,
                bid_levels=[(99.5, 5), (99.0, 10)],
                ask_levels=[(100.5, 5), (101.0, 10), (102.0, 20)],
            ),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 101.5, 0.1, OrderSide.BUY),
        ]
        # Buy 12 units → consumes 5 @ 100.5 + 7 @ 101.0
        # VWAP = (5*100.5 + 7*101.0) / 12 = 100.7917
        r = run_event_driven_backtest(
            _OneShotBuy(size=12.0), events, 100_000.0,
        )
        assert r.num_fills == 1
        f = r.fills[0]
        expected_vwap = (5 * 100.5 + 7 * 101.0) / 12
        assert f.fill_price == pytest.approx(expected_vwap, abs=1e-6)
        assert f.fill_size == pytest.approx(12.0)
        assert f.slippage_bps > 0  # unfavorable for buy

    def test_partial_fill_when_book_exhausted(self):
        events = [
            _deep_book(0, bid_levels=[(99.5, 5)], ask_levels=[(100.5, 3)]),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 100.5, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(_OneShotBuy(size=10.0), events, 100_000.0)
        assert r.num_fills == 1
        f = r.fills[0]
        assert f.fill_size == pytest.approx(3.0)
        assert f.partial is True


# ─── Limit orders ────────────────────────────────────────────────────


class TestLimitOrders:
    def test_marketable_limit_fills_immediately(self):
        """Limit buy at $101 with ask at $100.5 should fill at $101."""
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 100.5, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(
            _OneShotBuy(size=1.0, order_type=OrderType.LIMIT, limit_price=101.0),
            events,
            10_000.0,
        )
        assert r.num_fills == 1
        # Fills AT limit price (101), not the ask (100.5)
        assert r.fills[0].fill_price == pytest.approx(101.0)
        assert r.fills[0].slippage_bps == 0.0  # by limit convention

    def test_passive_limit_waits_for_cross(self):
        """Limit buy at $99 below market should rest until tick crosses."""
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            # Limit submitted; not yet crossed
            TickEvent(500, 100.0, 0.1, OrderSide.BUY),
            # Now a seller crosses the limit
            TickEvent(1000, 98.5, 0.1, OrderSide.SELL),
            TickEvent(2000, 99.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(
            _OneShotBuy(size=1.0, order_type=OrderType.LIMIT, limit_price=99.0),
            events,
            10_000.0,
        )
        assert r.num_fills == 1
        assert r.fills[0].fill_price == pytest.approx(99.0)
        assert r.fills[0].timestamp_ms == 1000  # at the crossing tick

    def test_passive_limit_never_fills_if_no_cross(self):
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(500, 100.0, 0.1, OrderSide.BUY),
            TickEvent(1000, 101.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 101.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(
            _OneShotBuy(size=1.0, order_type=OrderType.LIMIT, limit_price=95.0),
            events,
            10_000.0,
        )
        assert r.num_fills == 0
        assert r.final_position == 0.0


# ─── Book-at-arrival semantics ──────────────────────────────────────


class TestBookAtArrival:
    def test_market_order_fills_against_book_in_effect_at_arrival(self):
        """Order submitted at t=100, arrives at t=150, must NOT see book at t=500."""
        events = [
            _book(0, 99.5, 100.5),      # initial book
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),  # submit BUY here
            # Now there's a wide book gap — order should fill against OLD book
            _book(500, 199.5, 200.5),   # new book (far from old)
            TickEvent(1000, 200.0, 0.1, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(
            _OneShotBuy(size=1.0), events, 10_000.0,
            execution_simulator=ExecutionSimulator(latency_ms=50),
        )
        assert r.num_fills == 1
        # MUST fill at $100.5 (old book), NOT $200.5 (new book)
        assert r.fills[0].fill_price == pytest.approx(100.5)


# ─── on_fill callback ────────────────────────────────────────────────


class TestOnFillCallback:
    def test_strategy_receives_fill_notifications(self):
        seen_fills: list[FillEvent] = []

        class _Watcher(EventStrategy):
            def __init__(self):
                self.fired = False
            def on_tick(self, event, context):
                if not self.fired:
                    self.fired = True
                    return [OrderEvent(event.timestamp_ms, OrderSide.BUY, 1.0)]
                return []
            def on_fill(self, event, context):
                seen_fills.append(event)

        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 100.5, 0.1, OrderSide.BUY),
        ]
        run_event_driven_backtest(_Watcher(), events, 10_000.0)
        assert len(seen_fills) == 1
        assert seen_fills[0].side == OrderSide.BUY


# ─── Result summary ─────────────────────────────────────────────────


class TestResultSummary:
    def test_summary_renders_without_error(self):
        events = [
            _book(0, 99.5, 100.5),
            TickEvent(100, 100.0, 0.1, OrderSide.BUY),
            TickEvent(2000, 110.0, 1.0, OrderSide.BUY),
        ]
        r = run_event_driven_backtest(_OneShotBuy(size=1.0), events, 10_000.0)
        s = r.summary()
        assert "EventDrivenBacktestResult" in s
        assert "Initial equity" in s
        assert "Total PnL" in s

    def test_empty_event_stream(self):
        r = run_event_driven_backtest(_OneShotBuy(), [], 10_000.0)
        assert r.num_fills == 0
        assert r.total_pnl == 0.0
        assert r.final_equity == 10_000.0
