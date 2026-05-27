"""Signal aggregator — combines weak signals into ranked opportunities.

Uses weighted averaging with confidence scaling. Each signal contributes
proportional to its weight × confidence. The aggregated score determines
direction and conviction.

Inspired by Grinold's Fundamental Law of Active Management:
    IR = IC × sqrt(Breadth)
More uncorrelated signals (breadth) = stronger portfolio-level edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rift_engine.signals.base import SignalResult, compute_all_signals, get_all_signals


@dataclass
class Opportunity:
    """A ranked trading opportunity from the signal factory."""
    coin: str
    direction: str              # "LONG" or "SHORT"
    score: float                # aggregated score magnitude (0-1)
    raw_score: float            # signed score (-1 to +1)
    signals: list[dict]         # individual signal results
    num_signals: int            # how many signals contributed
    num_agreeing: int           # how many agree on direction
    confluence: float           # agreement ratio (0-1)
    categories: list[str]       # which signal categories fired

    # Optional price levels (computed externally by Scout)
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_reward: float = 0.0

    # Optional market context (attached by Scout for display)
    funding_rate: float = 0.0
    predicted_funding: float = 0.0
    volume_24h: float = 0.0
    atr_pct: float = 0.0
    hit_rate: float | None = None
    signal_names: list[str] = field(default_factory=list)
    num_categories: int = 0       # how many independent categories agree on direction

    # Mission brief (computed by Scout for Recon execution)
    leverage: int = 1                    # 1x, 2x, or 3x (max 3x for single-shot)
    size_pct: float = 0.02              # position size as % of equity
    hold_type: str = "momentum"          # exit-strategy label — Scout/aggregator derive from signal categories; "momentum" is the safety-net fallback recon handles
    staleness_minutes: int = 15         # opportunity expires after this many minutes
    confidence_tier: str = "low"        # "high" | "medium" | "low"

    # Validated edge (populated by Scout from backtest data)
    validated_strategy: str | None = None     # best proven strategy for this coin
    validated_sharpe: float | None = None     # historical Sharpe of that strategy
    validated_return_pct: float | None = None  # historical total return %


def aggregate_signals(
    coin: str,
    state: dict,
    hit_rates: dict | None = None,
) -> Opportunity | None:
    """Run all signals and aggregate into a single opportunity.

    Args:
        coin: Coin symbol (e.g., "BTC")
        state: Dict of market state (from StrategyState or live data)
        hit_rates: Optional dict of signal_name → historical hit rate
                   (from signal_memory). Boosts signals that historically work.

    Returns Opportunity or None if no meaningful signal.
    """
    # Import signal modules to trigger registration
    import rift_engine.signals.funding       # noqa: F401
    import rift_engine.signals.momentum      # noqa: F401
    import rift_engine.signals.microstructure  # noqa: F401
    import rift_engine.signals.volatility    # noqa: F401
    import rift_engine.signals.cross_pair    # noqa: F401
    import rift_engine.signals.seasonality   # noqa: F401

    results = compute_all_signals(coin, state)
    if not results:
        return None

    # Weight each signal: base_weight × confidence × hit_rate_bonus
    all_signals = get_all_signals()
    weighted_scores: list[float] = []
    weights: list[float] = []

    for r in results:
        sig = all_signals.get(r.name)
        base_weight = sig.weight if sig else 1.0

        # Historical hit rate bonus (from signal memory)
        hit_rate_mult = 1.0
        if hit_rates:
            hr = hit_rates.get(r.name)
            if hr is not None:
                hit_rate_mult = 0.5 + hr  # 0.5x at 0% hit rate, 1.5x at 100%

        total_weight = base_weight * r.confidence * hit_rate_mult
        weighted_scores.append(r.score * total_weight)
        weights.append(total_weight)

    if sum(weights) == 0:
        return None

    # Weighted average score (-1 to +1)
    raw_score = sum(weighted_scores) / sum(weights)

    # Minimum threshold — don't act on noise
    if abs(raw_score) < 0.1:
        return None

    direction = "LONG" if raw_score > 0 else "SHORT"

    # Count how many signals agree with the direction
    agreeing = sum(1 for r in results if (r.score > 0) == (raw_score > 0))
    total = len(results)
    confluence = agreeing / total if total > 0 else 0

    # Categories that fired
    categories = list(set(r.category for r in results))

    # Derive hold_type from dominant signal categories (see SIGNALS.md spec).
    # This is the documented behavior; callers can override post-construction.
    agreeing_cat_counts: dict[str, int] = {}
    for r in results:
        if (r.score > 0) == (raw_score > 0):
            agreeing_cat_counts[r.category] = agreeing_cat_counts.get(r.category, 0) + 1
    funding_cats = agreeing_cat_counts.get("funding", 0) + agreeing_cat_counts.get("seasonality", 0)
    vol_cats = agreeing_cat_counts.get("volatility", 0)
    if funding_cats >= 2:
        derived_hold_type = "funding"
    elif vol_cats >= 2:
        derived_hold_type = "mean_reversion"
    else:
        derived_hold_type = "momentum"

    return Opportunity(
        coin=coin,
        direction=direction,
        score=abs(raw_score),
        raw_score=raw_score,
        signals=[{
            "name": r.name,
            "score": round(r.score, 3),
            "reason": r.reason,
            "category": r.category,
            "confidence": round(r.confidence, 2),
        } for r in results],
        num_signals=total,
        num_agreeing=agreeing,
        confluence=round(confluence, 2),
        categories=categories,
        hold_type=derived_hold_type,
    )


def rank_opportunities(
    coins: list[str],
    states: dict[str, dict],
    hit_rates: dict | None = None,
    min_score: float = 0.15,
    min_signals: int = 2,
) -> list[Opportunity]:
    """Rank all coins by opportunity quality.

    Args:
        coins: List of coin symbols to scan
        states: Dict of coin → state dict
        hit_rates: Optional signal memory hit rates
        min_score: Minimum aggregated score to include
        min_signals: Minimum number of signals that must fire

    Returns sorted list of Opportunities (best first).
    """
    opportunities = []

    for coin in coins:
        state = states.get(coin, {})
        if not state:
            continue

        opp = aggregate_signals(coin, state, hit_rates)
        if opp and opp.score >= min_score and opp.num_signals >= min_signals:
            opportunities.append(opp)

    # Sort by score descending
    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities
