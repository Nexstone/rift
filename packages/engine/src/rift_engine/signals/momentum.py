"""Momentum signals — trend strength, RSI extremes, price acceleration."""

from rift_engine.signals.base import signal, SignalResult


@signal("rsi_extreme", "momentum", "RSI overbought/oversold extremes")
def rsi_extreme(coin: str, state: dict) -> SignalResult:
    indicators = state.get("indicators", {})
    # Try common RSI indicator names
    rsi = indicators.get("rsi", indicators.get("rsi_14", 50))
    if rsi == 0:
        return SignalResult("rsi_extreme", 0, "", "momentum", 0)

    if rsi < 25:
        score = min(1.0, (25 - rsi) / 25)
        return SignalResult("rsi_extreme", score, f"RSI {rsi:.0f} — deeply oversold", "momentum", 0.6)
    elif rsi < 30:
        return SignalResult("rsi_extreme", 0.4, f"RSI {rsi:.0f} — oversold", "momentum", 0.4)
    elif rsi > 75:
        score = -min(1.0, (rsi - 75) / 25)
        return SignalResult("rsi_extreme", score, f"RSI {rsi:.0f} — deeply overbought", "momentum", 0.6)
    elif rsi > 70:
        return SignalResult("rsi_extreme", -0.4, f"RSI {rsi:.0f} — overbought", "momentum", 0.4)

    return SignalResult("rsi_extreme", 0, "", "momentum", 0)


@signal("ema_trend", "momentum", "Price position relative to trend EMA")
def ema_trend(coin: str, state: dict) -> SignalResult:
    indicators = state.get("indicators", {})
    ema = indicators.get("ema_trend", indicators.get("ema", indicators.get("ema_100", 0)))
    price = state.get("price", state.get("close", 0))

    if ema == 0 or price == 0:
        return SignalResult("ema_trend", 0, "", "momentum", 0)

    deviation = (price - ema) / ema

    if deviation > 0.05:
        return SignalResult("ema_trend", 0.6, f"Price {deviation*100:+.1f}% above EMA — strong uptrend", "momentum", 0.5)
    elif deviation > 0.01:
        return SignalResult("ema_trend", 0.3, f"Price above EMA — uptrend", "momentum", 0.4)
    elif deviation < -0.05:
        return SignalResult("ema_trend", -0.6, f"Price {deviation*100:+.1f}% below EMA — strong downtrend", "momentum", 0.5)
    elif deviation < -0.01:
        return SignalResult("ema_trend", -0.3, f"Price below EMA — downtrend", "momentum", 0.4)

    return SignalResult("ema_trend", 0, "Price near EMA — no trend", "momentum", 0)


@signal("price_momentum", "momentum", "Short-term price rate of change")
def price_momentum(coin: str, state: dict) -> SignalResult:
    price_history = state.get("price_history", [])
    if len(price_history) < 10:
        return SignalResult("price_momentum", 0, "", "momentum", 0)

    # 10-period return
    current = price_history[-1]
    past = price_history[-10]
    if past == 0:
        return SignalResult("price_momentum", 0, "", "momentum", 0)

    ret = (current - past) / past

    if ret > 0.05:
        return SignalResult("price_momentum", 0.7, f"Strong momentum +{ret*100:.1f}%", "momentum", 0.5)
    elif ret > 0.02:
        return SignalResult("price_momentum", 0.4, f"Moderate momentum +{ret*100:.1f}%", "momentum", 0.4)
    elif ret < -0.05:
        return SignalResult("price_momentum", -0.7, f"Strong negative momentum {ret*100:.1f}%", "momentum", 0.5)
    elif ret < -0.02:
        return SignalResult("price_momentum", -0.4, f"Moderate negative momentum {ret*100:.1f}%", "momentum", 0.4)

    return SignalResult("price_momentum", 0, "", "momentum", 0)
