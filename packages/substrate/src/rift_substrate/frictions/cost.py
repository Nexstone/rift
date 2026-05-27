"""TradeCost — one-shot pre-trade cost estimate composing all the friction primitives.

Given a trade intent and market context, `estimate_trade_cost()` returns a
`TradeCost` with the breakdown:

  fees      from fees.estimate_fee()             — HL base + RIFT builder
  funding   from funding.expected_funding_cost() — over holding period
  impact    from an ImpactModel                  — predicted price impact in bps
  slippage  from slippage.walk_book()            — if book side is supplied

`total_bps` is the sum of all components in bps of notional. `total_usd` is
the dollar equivalent.

Two patterns:

  PRE-TRADE UX (no fills yet):
      cost = estimate_trade_cost(side="buy", notional_usd=10_000,
                                  mid_price=70_000, adv_usd=2.5e9,
                                  daily_vol=0.03, holding_period_hours=8)

  BACKTEST REALISM (per-fill):
      Use this to debit the strategy's PnL by the predicted cost. After
      the fact, compare to realized costs via tca / markouts to calibrate
      the impact model.

Side input accepts either trading-floor language ("buy"/"sell") or
position language ("long"/"short"); they're aliased internally.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from rift_substrate.frictions.fees import (
    FeeSchedule,
    estimate_fee,
)
from rift_substrate.frictions.funding import expected_funding_cost
from rift_substrate.frictions.impact import (
    ImpactModel,
    SqrtLawImpact,
)
from rift_substrate.frictions.slippage import L2Level, walk_book


def _direction_word(bps: float) -> str:
    """Map a signed bps value to 'cost', 'income', or '—'.

    Convention: positive bps = cost to the trader; negative = income.
    Values within ±0.005 bps of zero are rendered as '—'.
    """
    if not np.isfinite(bps):
        return "—"
    if abs(bps) < 0.005:
        return "—"
    return "cost" if bps > 0 else "income"


# Internal side normalization
_BUY_ALIASES = {"buy", "long"}
_SELL_ALIASES = {"sell", "short"}


def _normalize_side(side: str) -> tuple[str, str]:
    """Return (buy_or_sell, long_or_short) from any accepted input."""
    s = side.lower()
    if s in _BUY_ALIASES:
        return "buy", "long"
    if s in _SELL_ALIASES:
        return "sell", "short"
    raise ValueError(f"side must be one of {sorted(_BUY_ALIASES | _SELL_ALIASES)}; got {side!r}")


@dataclass(frozen=True)
class TradeCost:
    """Decomposed pre-trade cost estimate.

    All `*_bps` are bps of `notional_usd`. All `*_usd` are dollar costs.
    Positive = cost to the trader. Negative (rare) = income (e.g., maker
    rebate, funding income for a short in a high-funding regime).
    """

    side: str
    notional_usd: float

    fee_bps: float = 0.0
    fee_usd: float = 0.0

    funding_bps: float = 0.0
    funding_usd: float = 0.0

    impact_bps: float = 0.0
    impact_usd: float = 0.0

    slippage_bps: float = 0.0
    slippage_usd: float = 0.0

    total_bps: float = 0.0
    total_usd: float = 0.0

    # Diagnostic / lineage
    impact_model_name: str = ""
    book_filled_size: float = 0.0
    book_unfilled_size: float = 0.0

    def summary(self) -> str:
        """Render a human-readable cost breakdown.

        Uses explicit `cost` / `income` labels instead of signed numbers.
        Underlying signs in the dataclass fields still follow the convention
        "positive = cost, negative = income" (industry-standard for TCA), so
        callers doing math on the values get correct sums. The summary just
        shows magnitudes with directional words for readability.
        """
        rows = [
            ("Fees", self.fee_bps, self.fee_usd, ""),
            ("Funding", self.funding_bps, self.funding_usd, ""),
            (
                "Impact",
                self.impact_bps,
                self.impact_usd,
                f"  [{self.impact_model_name}]" if self.impact_model_name else "",
            ),
            ("Slippage", self.slippage_bps, self.slippage_usd, ""),
        ]

        # NET label switches between INCOME and COST based on direction.
        if abs(self.total_bps) < 0.005:
            net_label = "NET"
            net_dir = "—"
        elif self.total_bps < 0:
            net_label = "NET INCOME"
            net_dir = ""  # already in the label
        else:
            net_label = "NET COST"
            net_dir = ""

        lines = [
            f"TradeCost ({self.side}, ${self.notional_usd:,.2f} notional)",
            "─" * 60,
        ]
        for label, bps, usd, suffix in rows:
            direction = _direction_word(bps)
            lines.append(
                f"  {label:<10}  {abs(bps):>6.2f} bps  {direction:<7}   "
                f"${abs(usd):>9.2f}{suffix}"
            )
        lines.append("  " + "─" * 56)
        lines.append(
            f"  {net_label:<13} {abs(self.total_bps):>6.2f} bps  {net_dir:<7}   "
            f"${abs(self.total_usd):>9.2f}"
        )
        return "\n".join(lines)


def estimate_trade_cost(
    side: str,
    notional_usd: float,
    *,
    mid_price: float,
    adv_usd: float | None = None,
    daily_vol: float = 0.03,
    is_taker: bool = True,
    instrument: str = "perp",
    tier_volume_14d_usd: float = 0.0,
    include_builder_fee: bool = True,
    holding_period_hours: float = 0.0,
    current_funding_rate: float = 0.0,
    rate_drift_per_hour: float = 0.0,
    book_side: list[L2Level] | None = None,
    impact_model: ImpactModel | None = None,
    fee_schedule: FeeSchedule | None = None,
) -> TradeCost:
    """Compose fees + funding + impact + slippage into a single pre-trade estimate.

    Args:
      side:                   "buy"/"sell" or equivalently "long"/"short"
      notional_usd:           position $ notional
      mid_price:              current mid price (used for slippage walk + size→qty)
      adv_usd:                avg daily $ volume for impact estimation.
                              If None and no impact_model supplied, impact component = 0.
      daily_vol:              daily volatility (fractional). Used by sqrt-law impact.
      is_taker:               True if aggressing the book (default), False if posting
      instrument:             "perp" or "spot"
      tier_volume_14d_usd:    trailing 14d $ volume for fee tier
      include_builder_fee:    include the RIFT builder fee component
      holding_period_hours:   for funding projection. 0 = no funding accrual estimated.
      current_funding_rate:   per-interval funding rate to extrapolate
      rate_drift_per_hour:    optional drift on the funding rate (mean reversion)
      book_side:              optional L2 levels (for slippage walk)
                              For "buy", pass ASKS sorted ascending.
                              For "sell", pass BIDS sorted descending.
      impact_model:           ImpactModel instance. Default: SqrtLawImpact(γ=0.7)
      fee_schedule:           override the default FeeSchedule (else loads calibrations)

    Returns:
      `TradeCost` with bps + USD breakdown.
    """
    if notional_usd < 0:
        raise ValueError(f"notional_usd must be >= 0; got {notional_usd}")
    if mid_price <= 0:
        raise ValueError(f"mid_price must be > 0; got {mid_price}")

    buy_sell, long_short = _normalize_side(side)

    # ─── Fees ──────────────────────────────────────────────────────
    fee_quote = estimate_fee(
        notional_usd=notional_usd,
        is_taker=is_taker,
        instrument=instrument,
        tier_volume_14d_usd=tier_volume_14d_usd,
        include_builder_fee=include_builder_fee,
        schedule=fee_schedule,
    )

    # ─── Funding ───────────────────────────────────────────────────
    funding_usd = 0.0
    if holding_period_hours > 0:
        funding_usd = expected_funding_cost(
            position_side=long_short,
            notional_usd=notional_usd,
            current_rate=current_funding_rate,
            holding_period_hours=holding_period_hours,
            rate_drift_per_hour=rate_drift_per_hour,
        )
    funding_bps = (
        funding_usd / notional_usd * 10_000.0 if notional_usd > 0 else 0.0
    )

    # ─── Impact ────────────────────────────────────────────────────
    impact_bps = 0.0
    model_name = ""
    if impact_model is None and adv_usd is not None and adv_usd > 0:
        impact_model = SqrtLawImpact(gamma=0.7)
    if impact_model is not None and adv_usd is not None and adv_usd > 0:
        impact_bps = impact_model.predict_bps(
            trade_size_usd=notional_usd,
            adv_usd=adv_usd,
            daily_vol=daily_vol,
        )
        model_name = impact_model.name
        if not np.isfinite(impact_bps):
            impact_bps = 0.0
    impact_usd = notional_usd * impact_bps / 10_000.0

    # ─── Slippage (L2 walk) ───────────────────────────────────────
    slippage_bps = 0.0
    book_filled = 0.0
    book_unfilled = 0.0
    if book_side is not None and notional_usd > 0:
        size_qty = notional_usd / mid_price
        walk = walk_book(
            side=buy_sell,
            requested_size=size_qty,
            book_side=book_side,
            mid_price=mid_price,
        )
        book_filled = walk.filled_size
        book_unfilled = walk.unfilled_size
        if np.isfinite(walk.slippage_bps):
            slippage_bps = float(walk.slippage_bps)
    slippage_usd = notional_usd * slippage_bps / 10_000.0

    total_bps = float(fee_quote.total_bps + funding_bps + impact_bps + slippage_bps)
    total_usd = float(fee_quote.total_usd + funding_usd + impact_usd + slippage_usd)

    return TradeCost(
        side=buy_sell,
        notional_usd=notional_usd,
        fee_bps=float(fee_quote.total_bps),
        fee_usd=float(fee_quote.total_usd),
        funding_bps=float(funding_bps),
        funding_usd=float(funding_usd),
        impact_bps=float(impact_bps),
        impact_usd=float(impact_usd),
        slippage_bps=float(slippage_bps),
        slippage_usd=float(slippage_usd),
        total_bps=total_bps,
        total_usd=total_usd,
        impact_model_name=model_name,
        book_filled_size=float(book_filled),
        book_unfilled_size=float(book_unfilled),
    )
