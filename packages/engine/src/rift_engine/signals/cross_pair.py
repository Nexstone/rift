"""Cross-pair signals — relative value, lead-lag, correlation breakdown."""

from rift_engine.signals.base import signal, SignalResult


@signal("market_breadth", "cross_pair", "Cross-asset RSI breadth — crowd sentiment")
def market_breadth(coin: str, state: dict) -> SignalResult:
    ob = state.get("market_breadth_ob", 0)
    os_pct = state.get("market_breadth_os", 0)

    if ob == 0 and os_pct == 0:
        return SignalResult("market_breadth", 0, "", "cross_pair", 0)

    if ob > 60:
        score = -min(0.7, (ob - 50) / 50)
        return SignalResult("market_breadth", score, f"{ob:.0f}% of market overbought — overheated", "cross_pair", 0.5)
    elif os_pct > 60:
        score = min(0.7, (os_pct - 50) / 50)
        return SignalResult("market_breadth", score, f"{os_pct:.0f}% of market oversold — washed out", "cross_pair", 0.5)
    elif ob > 40:
        return SignalResult("market_breadth", -0.2, f"Market leaning overbought ({ob:.0f}%)", "cross_pair", 0.3)
    elif os_pct > 40:
        return SignalResult("market_breadth", 0.2, f"Market leaning oversold ({os_pct:.0f}%)", "cross_pair", 0.3)

    return SignalResult("market_breadth", 0, "", "cross_pair", 0)


@signal("avg_rsi_deviation", "cross_pair", "Coin RSI vs market average RSI — relative strength")
def avg_rsi_deviation(coin: str, state: dict) -> SignalResult:
    indicators = state.get("indicators", {})
    coin_rsi = indicators.get("rsi", indicators.get("rsi_14", 0))
    market_rsi = state.get("market_avg_rsi", 50)

    if coin_rsi == 0 or market_rsi == 0:
        return SignalResult("avg_rsi_deviation", 0, "", "cross_pair", 0)

    deviation = coin_rsi - market_rsi

    if deviation > 15:
        return SignalResult("avg_rsi_deviation", 0.5, f"Relative strength: RSI {coin_rsi:.0f} vs market {market_rsi:.0f}", "cross_pair", 0.4)
    elif deviation < -15:
        return SignalResult("avg_rsi_deviation", -0.5, f"Relative weakness: RSI {coin_rsi:.0f} vs market {market_rsi:.0f}", "cross_pair", 0.4)

    return SignalResult("avg_rsi_deviation", 0, "", "cross_pair", 0)


@signal("btc_lead_lag", "cross_pair", "BTC price movement leads altcoin movement by 5-30min")
def btc_lead_lag(coin: str, state: dict) -> SignalResult:
    """BTC moves first, alts follow. If BTC just moved and this alt hasn't, trade the catch-up."""
    if coin == "BTC":
        return SignalResult("btc_lead_lag", 0, "", "cross_pair", 0)

    btc_momentum = state.get("btc_momentum", 0)
    price_history = state.get("price_history", [])

    if btc_momentum == 0 or len(price_history) < 5:
        return SignalResult("btc_lead_lag", 0, "", "cross_pair", 0)

    coin_momentum = (price_history[-1] - price_history[-3]) / price_history[-3] if price_history[-3] != 0 else 0
    gap = btc_momentum - coin_momentum

    if btc_momentum > 0.01 and gap > 0.005:
        score = min(0.6, gap * 20)
        return SignalResult("btc_lead_lag", score,
            f"BTC up {btc_momentum*100:.1f}%, {coin} lagging — catch-up likely",
            "cross_pair", 0.5)
    elif btc_momentum < -0.01 and gap < -0.005:
        score = max(-0.6, gap * 20)
        return SignalResult("btc_lead_lag", score,
            f"BTC down {btc_momentum*100:.1f}%, {coin} lagging — catch-down likely",
            "cross_pair", 0.5)

    return SignalResult("btc_lead_lag", 0, "", "cross_pair", 0)


@signal("correlation_breakdown", "cross_pair", "Normally correlated pair diverging — convergence trade")
def correlation_breakdown(coin: str, state: dict) -> SignalResult:
    """When a coin deviates from its normal relationship with market, trade convergence."""
    market_avg_rsi = state.get("market_avg_rsi", 50)
    indicators = state.get("indicators", {})
    coin_rsi = indicators.get("rsi", indicators.get("rsi_14", 0))

    if coin_rsi == 0 or market_avg_rsi == 0:
        return SignalResult("correlation_breakdown", 0, "", "cross_pair", 0)

    deviation = coin_rsi - market_avg_rsi

    if deviation > 25:
        score = -min(0.5, (deviation - 20) / 40)
        return SignalResult("correlation_breakdown", score,
            f"{coin} RSI {coin_rsi:.0f} vs market {market_avg_rsi:.0f} — convergence short",
            "cross_pair", 0.4)
    elif deviation < -25:
        score = min(0.5, (abs(deviation) - 20) / 40)
        return SignalResult("correlation_breakdown", score,
            f"{coin} RSI {coin_rsi:.0f} vs market {market_avg_rsi:.0f} — convergence long",
            "cross_pair", 0.4)

    return SignalResult("correlation_breakdown", 0, "", "cross_pair", 0)
