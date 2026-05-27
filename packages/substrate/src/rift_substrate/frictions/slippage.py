"""L2 order book walk — predict slippage from a snapshot.

Given a side of the order book and a desired trade size, walk the levels
from best price outward, accumulating fills until the size is satisfied
(or the book is exhausted). Compute the VWAP fill price and report:

  - fill_vwap:       size-weighted average fill price
  - slippage_bps:    sign-adjusted gap vs. mid price (positive = unfavorable)
  - filled_size:     how much actually filled
  - unfilled_size:   remainder when the book ran out

This is the "what the book is telling you right now" cost — it ignores
queue position (you might not be first in line), market refill speed
(book might refresh before you finish), and adverse selection (the next
tick after you trade tends to move against you). For those, see
`frictions.impact` (empirical impact curve) and `frictions.markouts`
(post-trade adverse-selection diagnostic).

The walk is the practitioner-standard immediate-execution estimate. It
matches what an IOC marketable order would pay at the moment of the
snapshot — useful for pre-trade UX and for backtest realism on liquid
mid-frequency strategies.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class L2Level:
    """One level of the order book — price + size resting at that price."""

    price: float
    size: float


@dataclass(frozen=True)
class L2WalkResult:
    """Result of `walk_book()`.

    Attributes:
      side:               "buy" or "sell"
      requested_size:     how much the caller wanted to trade
      filled_size:        how much was filled by the walk
      unfilled_size:      requested - filled (book exhaustion)
      fill_vwap:          size-weighted average fill price (NaN if no fill)
      mid_price:          mid at the time of the snapshot
      slippage_bps:       sign-adjusted (positive = paid worse than mid).
                          NaN if no fill.
      n_levels_consumed:  number of book levels touched (1 if filled at best)
      levels_remaining:   levels not consumed (depth headroom)
    """

    side: str
    requested_size: float
    filled_size: float
    unfilled_size: float
    fill_vwap: float
    mid_price: float
    slippage_bps: float
    n_levels_consumed: int
    levels_remaining: int


def walk_book(
    side: str,
    requested_size: float,
    book_side: list[L2Level],
    mid_price: float,
) -> L2WalkResult:
    """Walk the book to fill `requested_size`, report the result.

    Args:
      side:           "buy" or "sell"
      requested_size: size to fill (positive)
      book_side:      for "buy", ASKS sorted ASCENDING (best=lowest first)
                      for "sell", BIDS sorted DESCENDING (best=highest first)
                      Caller is responsible for sorting; this function does
                      not validate ordering.
      mid_price:      mid price at the time of the snapshot

    Returns:
      `L2WalkResult` with fill VWAP, slippage in bps, and exhaustion info.

    Edge cases:
      - Empty book → no fill, NaN VWAP/slippage
      - First level larger than request → fill at the single level price
      - Book exhausted → partial fill; unfilled_size > 0
      - requested_size == 0 → trivially no fill
      - Invalid price (≤ 0) on a level → that level is skipped
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell'; got {side!r}")
    if requested_size < 0:
        raise ValueError(f"requested_size must be >= 0; got {requested_size}")
    if mid_price <= 0:
        raise ValueError(f"mid_price must be > 0; got {mid_price}")

    if requested_size == 0 or not book_side:
        return L2WalkResult(
            side=side,
            requested_size=requested_size,
            filled_size=0.0,
            unfilled_size=requested_size,
            fill_vwap=float("nan"),
            mid_price=mid_price,
            slippage_bps=float("nan"),
            n_levels_consumed=0,
            levels_remaining=len(book_side),
        )

    remaining = requested_size
    weighted_px = 0.0
    filled = 0.0
    n_consumed = 0

    for level in book_side:
        if level.price <= 0 or level.size <= 0:
            continue
        take = min(level.size, remaining)
        weighted_px += level.price * take
        filled += take
        remaining -= take
        n_consumed += 1
        if remaining <= 1e-12:
            break

    if filled <= 0:
        return L2WalkResult(
            side=side,
            requested_size=requested_size,
            filled_size=0.0,
            unfilled_size=requested_size,
            fill_vwap=float("nan"),
            mid_price=mid_price,
            slippage_bps=float("nan"),
            n_levels_consumed=0,
            levels_remaining=len(book_side),
        )

    vwap = weighted_px / filled
    # Slippage: buy fills should be ≥ mid; sell fills should be ≤ mid.
    # Convention: positive = unfavorable (cost).
    sign = 1.0 if side == "buy" else -1.0
    slippage_bps = sign * (vwap - mid_price) / mid_price * 10_000.0

    return L2WalkResult(
        side=side,
        requested_size=requested_size,
        filled_size=filled,
        unfilled_size=max(0.0, requested_size - filled),
        fill_vwap=vwap,
        mid_price=mid_price,
        slippage_bps=slippage_bps,
        n_levels_consumed=n_consumed,
        levels_remaining=len(book_side) - n_consumed,
    )
