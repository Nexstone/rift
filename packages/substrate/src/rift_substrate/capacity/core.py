"""Capacity analysis primitives.

Each `capacity_*` function returns a single max-trade-size in USD under one
constraint. `analyze_capacity()` runs all three, identifies the binding
constraint, and produces a capacity curve.

Design choice — binary-search vs. closed-form:
  For sqrt-law impact, capacity_impact has a closed-form solution. For the
  empirical fitter (and any future ImpactModel implementations), there's no
  closed form. We use bisection uniformly for consistency and to keep this
  module model-agnostic. The cost is a few dozen function evaluations per
  call — negligible against any I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from rift_substrate.frictions.impact import ImpactModel
from rift_substrate.frictions.slippage import L2Level, walk_book


# ─── Single-constraint primitives ─────────────────────────────────────


def capacity_impact(
    alpha_bps: float,
    impact_model: ImpactModel,
    adv_usd: float,
    daily_vol: float,
    max_impact_fraction: float = 0.5,
    search_upper_usd: float = 1e10,
) -> float:
    """Max trade size where impact stays under a fraction of alpha.

    The "half-alpha rule" (default 0.5) is the standard sustainable-trading
    heuristic: if impact eats half your alpha, the trade is borderline; more
    and you're paying the market for the privilege of trading.

    Args:
      alpha_bps:           expected alpha per trade in bps (must be > 0)
      impact_model:        any ImpactModel (sqrt-law or empirical)
      adv_usd:             average daily $ volume
      daily_vol:           daily fractional volatility (e.g., 0.025)
      max_impact_fraction: cap on impact_bps / alpha_bps (default 0.5)
      search_upper_usd:    bisection upper bound (default $10B — high enough
                           that for any realistic alpha + impact, this binds)

    Returns:
      Max trade size in USD. Returns 0 if alpha_bps ≤ 0. Returns
      `search_upper_usd` if no constraint binds within the search range
      (caller should investigate — likely alpha is much larger than impact
      even at extreme sizes, suggesting unrealistic inputs).
    """
    if alpha_bps <= 0:
        return 0.0
    target_impact = max_impact_fraction * alpha_bps
    if target_impact <= 0:
        return 0.0
    if adv_usd <= 0 or not np.isfinite(adv_usd):
        return 0.0

    # Sanity check: at zero size, impact is 0. If even at search_upper_usd,
    # impact is still below target, return upper (no constraint binds).
    upper_impact = impact_model.predict_bps(search_upper_usd, adv_usd, daily_vol)
    if not np.isfinite(upper_impact) or upper_impact <= target_impact:
        return float(search_upper_usd)

    # Bisection: find size where impact_bps(size) == target_impact
    lo, hi = 0.0, search_upper_usd
    for _ in range(64):  # 2^-64 of upper — plenty of precision
        mid = 0.5 * (lo + hi)
        impact = impact_model.predict_bps(mid, adv_usd, daily_vol)
        if not np.isfinite(impact):
            hi = mid
            continue
        if impact > target_impact:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-3:  # converged to sub-dollar precision
            break
    return float(lo)


def capacity_adv(adv_usd: float, max_pct: float = 0.05) -> float:
    """Max trade size as a fraction of average daily volume.

    Prudent defaults:
      0.01  — retail / quiet markets
      0.05  — MM / standard backtests (RIFT default)
      0.10  — institutional intraday
      0.20+ — willing to be the market-maker for the period

    Args:
      adv_usd:   average daily $ volume
      max_pct:   fraction of ADV (default 0.05 = 5%)

    Returns:
      Max trade size in USD. Returns 0 if ADV is non-positive or non-finite.
    """
    if adv_usd <= 0 or not np.isfinite(adv_usd):
        return 0.0
    if max_pct <= 0:
        return 0.0
    return float(adv_usd * max_pct)


def capacity_l2_depth(
    book_side: list[L2Level],
    mid_price: float,
    side: str,
    max_slippage_bps: float = 10.0,
) -> float:
    """Max trade size (USD notional) that fills within `max_slippage_bps` of mid.

    Walks the L2 book to find the largest BASE size whose VWAP slippage stays
    under the threshold, then returns the actual USD notional that trade would
    cost (base × fill_vwap). This is the *instantaneous* liquidity constraint —
    what you can do right now against the book as observed.

    Args:
      book_side:        ASKS (for a buy) or BIDS (for a sell), price-ordered
                        per L2Level convention
      mid_price:        reference mid
      side:             "buy" or "sell"
      max_slippage_bps: slippage tolerance (default 10 bps)

    Returns:
      Max trade size in USD (actual notional spent). Returns 0 if book is
      empty or mid is invalid.
    """
    if not book_side or mid_price <= 0 or not np.isfinite(mid_price):
        return 0.0
    if max_slippage_bps <= 0:
        return 0.0

    # Maximum base size = total size available on the book
    total_base = sum(lvl.size for lvl in book_side)
    if total_base <= 0:
        return 0.0

    def _walk(base_size: float):
        return walk_book(side, base_size, book_side, mid_price)

    # If the whole book fills within tolerance, return its full notional
    full_walk = _walk(total_base)
    if full_walk.filled_size > 0 and abs(full_walk.slippage_bps) <= max_slippage_bps:
        return float(full_walk.filled_size * full_walk.fill_vwap)

    # Otherwise bisect on base size
    lo_base, hi_base = 0.0, total_base
    best_usd = 0.0
    for _ in range(64):
        mid_base = 0.5 * (lo_base + hi_base)
        if mid_base <= 0:
            break
        w = _walk(mid_base)
        # Accept if fully filled AND within tolerance
        if (
            w.filled_size > 0
            and w.unfilled_size <= 1e-12
            and abs(w.slippage_bps) <= max_slippage_bps
        ):
            best_usd = w.filled_size * w.fill_vwap
            lo_base = mid_base
        else:
            hi_base = mid_base
        if hi_base - lo_base < 1e-12:
            break
    return float(best_usd)


# ─── Composite analysis ──────────────────────────────────────────────


@dataclass(frozen=True)
class CapacityCurvePoint:
    """One point on the capacity curve: trade size → expected impact + net alpha."""

    trade_size_usd: float
    impact_bps: float
    net_alpha_bps: float  # alpha_bps - impact_bps


@dataclass(frozen=True)
class CapacityResult:
    """Aggregate capacity analysis output.

    Attributes:
      max_trade_size_usd:    binding-constraint min of the three
      binding_constraint:    "impact" | "adv" | "l2_depth"
      impact_constraint_usd: max size under the impact rule (alpha-aware)
      adv_constraint_usd:    max size under the ADV rule
      l2_constraint_usd:     max size under the L2-depth rule (NaN if no book provided)
      capacity_curve:        list of CapacityCurvePoint sorted by trade_size_usd
      half_alpha_size_usd:   size where impact == 0.5 × alpha (the standard
                             "sustainable" benchmark — same as impact_constraint
                             with max_impact_fraction=0.5)
      breakeven_size_usd:    size where impact == alpha (net alpha = 0 — the
                             *theoretical* max where the strategy still pays
                             for itself, before any safety margin)
      alpha_bps:             alpha-per-trade input (echo for self-description)
      adv_usd:               ADV input (echo)
      daily_vol:             vol input (echo)
    """

    max_trade_size_usd: float
    binding_constraint: str
    impact_constraint_usd: float
    adv_constraint_usd: float
    l2_constraint_usd: float
    capacity_curve: list[CapacityCurvePoint] = field(default_factory=list)
    half_alpha_size_usd: float = 0.0
    breakeven_size_usd: float = 0.0
    alpha_bps: float = 0.0
    adv_usd: float = 0.0
    daily_vol: float = 0.0

    def summary(self) -> str:
        def _fmt_usd(x: float) -> str:
            if not np.isfinite(x):
                return "  n/a   "
            if x >= 1e9:
                return f"${x / 1e9:>6.2f}B"
            if x >= 1e6:
                return f"${x / 1e6:>6.2f}M"
            if x >= 1e3:
                return f"${x / 1e3:>6.2f}K"
            return f"${x:>7.2f}"

        lines = [
            "CapacityResult",
            "─" * 56,
            f"  Alpha:               {self.alpha_bps:>6.1f} bps/trade",
            f"  ADV:                 {_fmt_usd(self.adv_usd)}",
            f"  Daily vol:           {self.daily_vol * 100:>6.2f}%",
            "",
            f"  Max trade size:      {_fmt_usd(self.max_trade_size_usd)}  ← {self.binding_constraint}",
            "",
            "  Constraints:",
            f"    Impact (½α):       {_fmt_usd(self.impact_constraint_usd)}",
            f"    ADV-based:         {_fmt_usd(self.adv_constraint_usd)}",
            f"    L2 depth:          {_fmt_usd(self.l2_constraint_usd)}",
            "",
            "  Reference points:",
            f"    Half-alpha size:   {_fmt_usd(self.half_alpha_size_usd)}",
            f"    Breakeven size:    {_fmt_usd(self.breakeven_size_usd)}",
        ]
        return "\n".join(lines)


def analyze_capacity(
    alpha_bps: float,
    impact_model: ImpactModel,
    adv_usd: float,
    daily_vol: float,
    *,
    book_side: list[L2Level] | None = None,
    mid_price: float | None = None,
    side: str = "buy",
    max_impact_fraction: float = 0.5,
    max_adv_pct: float = 0.05,
    max_slippage_bps: float = 10.0,
    n_curve_points: int = 30,
) -> CapacityResult:
    """Run all three capacity constraints and return a composite result.

    Args:
      alpha_bps:           expected alpha per trade in bps
      impact_model:        ImpactModel (sqrt-law, empirical, or custom)
      adv_usd:             average daily $ volume
      daily_vol:           daily fractional volatility
      book_side:           optional L2 book (asks for buys, bids for sells)
      mid_price:           optional mid (required if book_side given)
      side:                "buy" or "sell" (matches book_side)
      max_impact_fraction: alpha fraction for impact constraint (default 0.5)
      max_adv_pct:         ADV fraction for the ADV constraint (default 0.05)
      max_slippage_bps:    slippage tol for the L2 constraint (default 10 bps)
      n_curve_points:      points on the capacity curve (default 30)

    Returns:
      CapacityResult with all three constraints, binding-constraint label,
      and a capacity curve from $0 up to ~2× the binding constraint.
    """
    # 1. Impact constraint
    impact_cap = capacity_impact(
        alpha_bps=alpha_bps,
        impact_model=impact_model,
        adv_usd=adv_usd,
        daily_vol=daily_vol,
        max_impact_fraction=max_impact_fraction,
    )

    # 2. ADV constraint
    adv_cap = capacity_adv(adv_usd=adv_usd, max_pct=max_adv_pct)

    # 3. L2-depth constraint (only if book provided)
    if book_side is not None and mid_price is not None and len(book_side) > 0:
        l2_cap = capacity_l2_depth(
            book_side=book_side,
            mid_price=mid_price,
            side=side,
            max_slippage_bps=max_slippage_bps,
        )
    else:
        l2_cap = float("nan")

    # Binding constraint = min of finite constraints
    candidates = {
        "impact": impact_cap,
        "adv": adv_cap,
    }
    if np.isfinite(l2_cap):
        candidates["l2_depth"] = l2_cap

    binding_name = min(candidates, key=lambda k: candidates[k])
    max_size = candidates[binding_name]

    # Reference points (half-alpha already == impact_cap; compute breakeven)
    half_alpha_size = impact_cap
    breakeven_size = capacity_impact(
        alpha_bps=alpha_bps,
        impact_model=impact_model,
        adv_usd=adv_usd,
        daily_vol=daily_vol,
        max_impact_fraction=1.0,  # impact == alpha
    )

    # Capacity curve: span [0, max(2× binding, 1.5× breakeven)] so the user sees
    # both the sustainable region (net alpha > 0) and the deep-impact region
    # (net alpha < 0). Without this, the curve can be entirely positive if the
    # binding constraint is the half-alpha rule (which is < breakeven).
    curve_upper = max(max_size * 2.0, breakeven_size * 1.5, 1.0)
    sizes = np.linspace(0.0, curve_upper, n_curve_points + 1)[1:]  # drop the 0
    curve: list[CapacityCurvePoint] = []
    for s in sizes:
        impact_bps = impact_model.predict_bps(float(s), adv_usd, daily_vol)
        if not np.isfinite(impact_bps):
            impact_bps = float("nan")
        net_alpha = alpha_bps - impact_bps if np.isfinite(impact_bps) else float("nan")
        curve.append(CapacityCurvePoint(
            trade_size_usd=float(s),
            impact_bps=float(impact_bps),
            net_alpha_bps=float(net_alpha),
        ))

    return CapacityResult(
        max_trade_size_usd=float(max_size),
        binding_constraint=binding_name,
        impact_constraint_usd=float(impact_cap),
        adv_constraint_usd=float(adv_cap),
        l2_constraint_usd=float(l2_cap),
        capacity_curve=curve,
        half_alpha_size_usd=float(half_alpha_size),
        breakeven_size_usd=float(breakeven_size),
        alpha_bps=float(alpha_bps),
        adv_usd=float(adv_usd),
        daily_vol=float(daily_vol),
    )
