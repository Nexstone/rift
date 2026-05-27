"""HyperStats signals — from scraped L/S ratios, leverage, and unrealized PnL.

These signals use data collected by our HyperStats scraper (hyperstats_scraper.py).
If the data isn't available, signals return 0 (dormant but ready).
"""

from rift_engine.signals.base import signal, SignalResult


@signal("ls_ratio_extreme", "hyperstats", "Long/short trader ratio extreme — crowd positioning")
def ls_ratio_extreme(coin: str, state: dict) -> SignalResult:
    """When 80%+ of traders are on one side, retail is crowded = contrarian signal."""
    ls_ratio = state.get("ls_ratio", 0)  # >1 = more longs, <1 = more shorts

    if ls_ratio == 0:
        return SignalResult("ls_ratio_extreme", 0, "", "hyperstats", 0)

    # Convert ratio to percentage
    if ls_ratio > 1:
        long_pct = ls_ratio / (1 + ls_ratio) * 100
    else:
        long_pct = ls_ratio / (1 + ls_ratio) * 100

    if long_pct > 75:
        score = -min(0.6, (long_pct - 65) / 50)
        return SignalResult("ls_ratio_extreme", score,
            f"{long_pct:.0f}% of traders are long — retail overcrowded",
            "hyperstats", 0.5)
    elif long_pct < 25:
        score = min(0.6, (65 - long_pct) / 50)
        return SignalResult("ls_ratio_extreme", score,
            f"Only {long_pct:.0f}% long — shorts overcrowded",
            "hyperstats", 0.5)

    return SignalResult("ls_ratio_extreme", 0, "", "hyperstats", 0)


@signal("leverage_extreme", "hyperstats", "Average leverage spike — fragile market")
def leverage_extreme(coin: str, state: dict) -> SignalResult:
    """When average leverage spikes, the market is fragile and liquidation cascades are likely."""
    avg_leverage = state.get("avg_leverage", 0)

    if avg_leverage == 0:
        return SignalResult("leverage_extreme", 0, "", "hyperstats", 0)

    # Normal leverage: 2-5x. Elevated: 5-10x. Extreme: >10x.
    if avg_leverage > 10:
        # Extreme leverage — market is a powder keg
        # Don't take direction, just signal danger (reduce all positions)
        return SignalResult("leverage_extreme", 0, f"Avg leverage {avg_leverage:.1f}x — extreme fragility", "hyperstats", 0.2)
    elif avg_leverage > 7:
        return SignalResult("leverage_extreme", 0, f"Avg leverage {avg_leverage:.1f}x — elevated risk", "hyperstats", 0.1)

    return SignalResult("leverage_extreme", 0, "", "hyperstats", 0)


@signal("unrealized_pnl", "hyperstats", "Aggregate unrealized P&L — profit-taking or panic risk")
def unrealized_pnl(coin: str, state: dict) -> SignalResult:
    """When most traders are underwater, panic selling likely. When profitable, profit-taking likely."""
    unrealized = state.get("aggregate_unrealized_pnl", 0)

    if unrealized == 0:
        return SignalResult("unrealized_pnl", 0, "", "hyperstats", 0)

    # Positive unrealized = most traders in profit = profit-taking risk = bearish
    # Negative unrealized = most traders underwater = capitulation risk then reversal = bullish
    if unrealized > 0:
        # Traders in profit — expect profit-taking
        return SignalResult("unrealized_pnl", -0.3,
            "Aggregate unrealized PnL positive — profit-taking risk",
            "hyperstats", 0.4)
    else:
        # Traders underwater — expect capitulation then reversal
        return SignalResult("unrealized_pnl", 0.3,
            "Aggregate unrealized PnL negative — capitulation/reversal likely",
            "hyperstats", 0.4)
