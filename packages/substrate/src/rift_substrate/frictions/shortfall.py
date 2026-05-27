"""Implementation Shortfall — Perold (1988).

Decomposes the gap between a "paper portfolio" (priced at decision time)
and the actual realized portfolio into four components:

  timing cost       — paid above decision price on filled portion (delay cost +
                      market impact from your own trades + adverse drift)
  market impact     — already-rolled into timing cost in the basic Perold formula;
                      reported separately when we have a `final_mid_price` snapshot
                      that excludes the trader's own impact (provide via L2 walk
                      or pre-trade snapshot)
  opportunity cost  — paid on the unfilled portion (the part of the parent order
                      that never filled and missed the move)
  commission        — fees paid (taker fees + builder fee + maker rebates)

  total shortfall = timing + impact + opportunity + commission

Sign convention: positive = cost (worse than the paper portfolio).
For a buy: timing > 0 means you paid above decision.
For a sell: timing > 0 means you sold below decision.

Reference:
  Perold, A. F. (1988). "The Implementation Shortfall: Paper Versus Reality."
    Journal of Portfolio Management 14(3), 4-9.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Fill:
    """One fill of a parent order."""

    timestamp_ms: int
    price: float
    size: float         # absolute size (always positive)
    fee_usd: float = 0.0


@dataclass(frozen=True)
class ImplementationShortfall:
    """Perold-style decomposition of trade cost vs. the paper portfolio.

    All `*_usd` fields are in dollars; signs follow the cost convention
    (positive = unfavorable). All `*_bps` fields are bps of `intended_notional_usd`.
    """

    side: str                          # "buy" or "sell"
    decision_price: float              # mid price at decision time
    final_mid_price: float             # mid price after the parent order finished
    intended_size: float               # total size the parent order tried to execute
    filled_size: float                 # total filled
    unfilled_size: float               # intended - filled
    average_fill_price: float          # VWAP of fills (NaN if no fills)
    intended_notional_usd: float       # intended_size * decision_price
    filled_notional_usd: float         # filled_size * average_fill_price

    # USD components
    timing_cost_usd: float
    opportunity_cost_usd: float
    commission_usd: float
    total_shortfall_usd: float

    # Bps components (of intended_notional_usd)
    timing_cost_bps: float = field(default=float("nan"))
    opportunity_cost_bps: float = field(default=float("nan"))
    commission_bps: float = field(default=float("nan"))
    total_shortfall_bps: float = field(default=float("nan"))

    def summary(self) -> str:
        lines = [
            f"Implementation Shortfall ({self.side}, decision = ${self.decision_price:,.6g})",
            "  Convention: positive = cost vs. paper portfolio. Negative would mean",
            "              execution beat the benchmark (the market moved in your favor",
            "              during the implementation delay).",
            "─" * 70,
            f"  Intended:     {self.intended_size:.6g} @ ${self.decision_price:,.6g}"
            f"   (${self.intended_notional_usd:,.2f})",
            f"  Filled:       {self.filled_size:.6g} @ ${self.average_fill_price:,.6g}"
            f"   (${self.filled_notional_usd:,.2f})",
            f"  Unfilled:     {self.unfilled_size:.6g}",
            f"  Final mid:    ${self.final_mid_price:,.6g}",
            "",
            f"  Timing cost:        ${self.timing_cost_usd:>+12,.2f}   ({self.timing_cost_bps:>+8.2f} bps)",
            f"  Opportunity cost:   ${self.opportunity_cost_usd:>+12,.2f}   ({self.opportunity_cost_bps:>+8.2f} bps)",
            f"  Commission:         ${self.commission_usd:>+12,.2f}   ({self.commission_bps:>+8.2f} bps)",
            "  " + "─" * 60,
            f"  TOTAL SHORTFALL:    ${self.total_shortfall_usd:>+12,.2f}   ({self.total_shortfall_bps:>+8.2f} bps)",
        ]
        return "\n".join(lines)


def implementation_shortfall(
    side: str,
    decision_price: float,
    intended_size: float,
    fills: list[Fill],
    final_mid_price: float,
) -> ImplementationShortfall:
    """Compute Perold's implementation shortfall for a parent order.

    Args:
      side:             "buy" or "sell"
      decision_price:   mid price at the moment the parent order was decided
      intended_size:    size the parent order was intended to fill (absolute, > 0)
      fills:            list of Fill objects — child fills that actually happened
      final_mid_price:  mid price after the parent order finished (or was cancelled);
                        used to value the unfilled portion (opportunity cost)

    Returns:
      `ImplementationShortfall` with USD + bps decomposition.

    Math:
      For a BUY of intended_size at decision_price, paper portfolio value at
      final_mid_price = intended_size * (final_mid_price - decision_price).

      Realized value =
          + filled_size * (final_mid_price - average_fill_price)   ← P&L on what filled
          - commission                                              ← fees

      Shortfall = paper - realized
                = intended_size * (final_mid - decision)
                  - filled_size * (final_mid - avg_fill)
                  + commission
                = filled_size * (avg_fill - decision)   ← timing cost
                  + unfilled_size * (final_mid - decision)   ← opportunity cost
                  + commission

      For a SELL, signs flip.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell'; got {side!r}")
    if intended_size <= 0:
        raise ValueError(f"intended_size must be > 0; got {intended_size}")
    if decision_price <= 0 or not np.isfinite(decision_price):
        raise ValueError(f"decision_price must be positive finite; got {decision_price}")

    filled_size = float(sum(f.size for f in fills))
    if filled_size > intended_size + 1e-9:
        raise ValueError(
            f"filled ({filled_size}) > intended ({intended_size}) — over-fill not allowed"
        )
    unfilled_size = max(0.0, intended_size - filled_size)
    commission_usd = float(sum(f.fee_usd for f in fills))

    if filled_size > 0:
        weighted_px = sum(f.price * f.size for f in fills)
        average_fill_price = float(weighted_px / filled_size)
    else:
        average_fill_price = float("nan")

    intended_notional_usd = intended_size * decision_price
    filled_notional_usd = filled_size * average_fill_price if filled_size > 0 else 0.0

    # Side multiplier: +1 for buy, -1 for sell. Costs are positive in both cases.
    sign = 1.0 if side == "buy" else -1.0

    # Timing cost: filled portion priced relative to decision.
    if filled_size > 0:
        timing_cost_usd = sign * filled_size * (average_fill_price - decision_price)
    else:
        timing_cost_usd = 0.0

    # Opportunity cost: unfilled portion priced at final mid relative to decision.
    opportunity_cost_usd = sign * unfilled_size * (final_mid_price - decision_price)

    total_shortfall_usd = timing_cost_usd + opportunity_cost_usd + commission_usd

    bps_factor = 10_000.0 / intended_notional_usd if intended_notional_usd > 0 else float("nan")

    return ImplementationShortfall(
        side=side,
        decision_price=decision_price,
        final_mid_price=final_mid_price,
        intended_size=intended_size,
        filled_size=filled_size,
        unfilled_size=unfilled_size,
        average_fill_price=average_fill_price,
        intended_notional_usd=intended_notional_usd,
        filled_notional_usd=filled_notional_usd,
        timing_cost_usd=timing_cost_usd,
        opportunity_cost_usd=opportunity_cost_usd,
        commission_usd=commission_usd,
        total_shortfall_usd=total_shortfall_usd,
        timing_cost_bps=timing_cost_usd * bps_factor,
        opportunity_cost_bps=opportunity_cost_usd * bps_factor,
        commission_bps=commission_usd * bps_factor,
        total_shortfall_bps=total_shortfall_usd * bps_factor,
    )
