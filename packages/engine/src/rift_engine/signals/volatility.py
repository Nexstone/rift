"""Volatility signals — squeeze detection, vol mean reversion, expansion."""

from rift_engine.signals.base import signal, SignalResult


@signal("vol_mean_reversion", "volatility", "Volatility deviation from rolling mean")
def vol_mean_reversion(coin: str, state: dict) -> SignalResult:
    indicators = state.get("indicators", {})
    bb_width = indicators.get("bb_width", 0)

    if bb_width == 0:
        return SignalResult("vol_mean_reversion", 0, "", "volatility", 0)

    # Narrow BB width = low vol = expansion coming (direction unknown)
    # Wide BB width = high vol = contraction coming
    # We score direction from price relative to BB midline
    bb_upper = indicators.get("bb_upper", 0)
    bb_lower = indicators.get("bb_lower", 0)
    price = state.get("price", state.get("close", 0))

    if bb_upper == 0 or price == 0:
        return SignalResult("vol_mean_reversion", 0, "", "volatility", 0)

    bb_mid = (bb_upper + bb_lower) / 2
    bb_range = bb_upper - bb_lower
    if bb_range <= 0:
        return SignalResult("vol_mean_reversion", 0, "", "volatility", 0)

    position_in_band = (price - bb_lower) / bb_range  # 0 = at lower, 1 = at upper

    if position_in_band > 0.9:
        return SignalResult("vol_mean_reversion", -0.5, "Price at upper BB — vol reversion likely downward", "volatility", 0.4)
    elif position_in_band < 0.1:
        return SignalResult("vol_mean_reversion", 0.5, "Price at lower BB — vol reversion likely upward", "volatility", 0.4)

    return SignalResult("vol_mean_reversion", 0, "", "volatility", 0)


@signal("squeeze_detection", "volatility", "Bollinger Band inside Keltner Channel — compression")
def squeeze_detection(coin: str, state: dict) -> SignalResult:
    indicators = state.get("indicators", {})
    bb_upper = indicators.get("bb_upper", 0)
    bb_lower = indicators.get("bb_lower", 0)
    kelt_upper = indicators.get("kelt_upper", 0)
    kelt_lower = indicators.get("kelt_lower", 0)

    if bb_upper == 0 or kelt_upper == 0:
        return SignalResult("squeeze_detection", 0, "", "volatility", 0)

    in_squeeze = bb_upper < kelt_upper and bb_lower > kelt_lower
    price = state.get("price", state.get("close", 0))
    ema = indicators.get("ema", indicators.get("ema_trend", 0))

    if in_squeeze:
        # Squeeze active — score direction from price vs EMA
        if ema > 0 and price > ema:
            return SignalResult("squeeze_detection", 0.4, "Squeeze active — bullish bias (above EMA)", "volatility", 0.5)
        elif ema > 0:
            return SignalResult("squeeze_detection", -0.4, "Squeeze active — bearish bias (below EMA)", "volatility", 0.5)
        return SignalResult("squeeze_detection", 0.1, "Squeeze active — direction unclear", "volatility", 0.3)

    return SignalResult("squeeze_detection", 0, "", "volatility", 0)


@signal("premium_extreme", "volatility", "Mark vs oracle premium — market overheating")
def premium_extreme(coin: str, state: dict) -> SignalResult:
    premium = state.get("premium", 0)

    if abs(premium) < 0.001:
        return SignalResult("premium_extreme", 0, "", "volatility", 0)

    # High premium = mark above oracle = overleveraged longs = reversion short
    # Low premium = mark below oracle = overleveraged shorts = reversion long
    if premium > 0.003:
        score = -min(0.8, premium / 0.005)
        return SignalResult("premium_extreme", score, f"Premium +{premium*100:.2f}% — market overheated", "volatility", 0.6)
    elif premium < -0.003:
        score = min(0.8, abs(premium) / 0.005)
        return SignalResult("premium_extreme", score, f"Premium {premium*100:.2f}% — market oversold", "volatility", 0.6)
    elif premium > 0.001:
        return SignalResult("premium_extreme", -0.2, f"Premium mildly elevated", "volatility", 0.3)
    else:
        return SignalResult("premium_extreme", 0.2, f"Premium mildly depressed", "volatility", 0.3)
