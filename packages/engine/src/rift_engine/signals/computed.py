"""Computed signals — derived from existing price/volume data using pure math.

No new data collection needed. These extract hidden information from
data we already have: trend persistence, fat tails, serial correlation.
"""

import math
from rift_engine.signals.base import signal, SignalResult


@signal("hurst_exponent", "computed", "Trend persistence vs mean reversion tendency")
def hurst_exponent(coin: str, state: dict) -> SignalResult:
    """Hurst exponent: >0.5 = trending, <0.5 = mean-reverting, =0.5 = random.

    Tells you WHICH strategy type to apply, not direction.
    Score is directional: positive = trending (use momentum), negative = mean-reverting (use reversion).
    """
    price_history = state.get("price_history", [])
    if len(price_history) < 20:
        return SignalResult("hurst_exponent", 0, "", "computed", 0)

    # Simplified R/S analysis on returns
    prices = price_history[-20:]
    returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices)) if prices[i-1] != 0]
    if len(returns) < 10:
        return SignalResult("hurst_exponent", 0, "", "computed", 0)

    n = len(returns)
    mean_r = sum(returns) / n
    deviations = [r - mean_r for r in returns]

    # Cumulative deviations
    cumdev = []
    s = 0
    for d in deviations:
        s += d
        cumdev.append(s)

    R = max(cumdev) - min(cumdev)
    S = (sum(d**2 for d in deviations) / n) ** 0.5

    if S == 0:
        return SignalResult("hurst_exponent", 0, "", "computed", 0)

    rs = R / S
    if rs <= 0:
        return SignalResult("hurst_exponent", 0, "", "computed", 0)

    H = math.log(rs) / math.log(n)
    H = max(0, min(1, H))

    # Get current price direction for scoring
    recent_dir = prices[-1] - prices[-5] if len(prices) >= 5 else 0

    if H > 0.6:
        # Trending — score in direction of current trend
        if recent_dir > 0:
            return SignalResult("hurst_exponent", 0.4, f"Hurst {H:.2f} — trending (bullish momentum)", "computed", 0.5)
        else:
            return SignalResult("hurst_exponent", -0.4, f"Hurst {H:.2f} — trending (bearish momentum)", "computed", 0.5)
    elif H < 0.4:
        # Mean reverting — score AGAINST current direction
        if recent_dir > 0:
            return SignalResult("hurst_exponent", -0.3, f"Hurst {H:.2f} — mean-reverting (overbought)", "computed", 0.4)
        else:
            return SignalResult("hurst_exponent", 0.3, f"Hurst {H:.2f} — mean-reverting (oversold)", "computed", 0.4)

    return SignalResult("hurst_exponent", 0, f"Hurst {H:.2f} — random walk", "computed", 0)


@signal("return_autocorrelation", "computed", "Serial correlation of returns — momentum vs reversal")
def return_autocorrelation(coin: str, state: dict) -> SignalResult:
    """Positive autocorrelation = momentum works. Negative = mean reversion works."""
    price_history = state.get("price_history", [])
    if len(price_history) < 15:
        return SignalResult("return_autocorrelation", 0, "", "computed", 0)

    prices = price_history[-15:]
    returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices)) if prices[i-1] != 0]
    if len(returns) < 10:
        return SignalResult("return_autocorrelation", 0, "", "computed", 0)

    # Lag-1 autocorrelation
    n = len(returns)
    mean_r = sum(returns) / n
    var = sum((r - mean_r)**2 for r in returns) / n
    if var == 0:
        return SignalResult("return_autocorrelation", 0, "", "computed", 0)

    cov = sum((returns[i] - mean_r) * (returns[i-1] - mean_r) for i in range(1, n)) / (n - 1)
    autocorr = cov / var

    recent_dir = prices[-1] - prices[-3]

    if autocorr > 0.3:
        # Strong positive autocorrelation — momentum regime
        if recent_dir > 0:
            return SignalResult("return_autocorrelation", 0.4, f"Autocorr {autocorr:.2f} — momentum bullish", "computed", 0.4)
        else:
            return SignalResult("return_autocorrelation", -0.4, f"Autocorr {autocorr:.2f} — momentum bearish", "computed", 0.4)
    elif autocorr < -0.3:
        # Strong negative autocorrelation — reversal regime
        if recent_dir > 0:
            return SignalResult("return_autocorrelation", -0.3, f"Autocorr {autocorr:.2f} — reversal expected (was up)", "computed", 0.4)
        else:
            return SignalResult("return_autocorrelation", 0.3, f"Autocorr {autocorr:.2f} — reversal expected (was down)", "computed", 0.4)

    return SignalResult("return_autocorrelation", 0, "", "computed", 0)


@signal("return_kurtosis", "computed", "Fat tail detection — expect extreme moves")
def return_kurtosis(coin: str, state: dict) -> SignalResult:
    """High kurtosis = fat tails = extreme moves likely. Affects confidence, not direction."""
    price_history = state.get("price_history", [])
    if len(price_history) < 20:
        return SignalResult("return_kurtosis", 0, "", "computed", 0)

    prices = price_history[-20:]
    returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices)) if prices[i-1] != 0]
    if len(returns) < 10:
        return SignalResult("return_kurtosis", 0, "", "computed", 0)

    n = len(returns)
    mean_r = sum(returns) / n
    std_r = (sum((r - mean_r)**2 for r in returns) / n) ** 0.5
    if std_r == 0:
        return SignalResult("return_kurtosis", 0, "", "computed", 0)

    kurt = sum(((r - mean_r) / std_r)**4 for r in returns) / n - 3  # excess kurtosis

    if kurt > 3:
        # Very fat tails — extreme move coming, reduce all signal confidence
        return SignalResult("return_kurtosis", 0, f"Kurtosis {kurt:.1f} — extreme tail risk, reduce size", "computed", 0.2)
    elif kurt > 1:
        return SignalResult("return_kurtosis", 0, f"Kurtosis {kurt:.1f} — elevated tail risk", "computed", 0.1)

    return SignalResult("return_kurtosis", 0, "", "computed", 0)


@signal("oi_acceleration", "computed", "OI rate of change acceleration — institutional accumulation")
def oi_acceleration(coin: str, state: dict) -> SignalResult:
    """Second derivative of OI. Accelerating OI = institutional accumulation phase."""
    oi_roc = state.get("oi_roc", 0)
    oi_delta = state.get("oi_delta", 0)

    if oi_roc == 0:
        return SignalResult("oi_acceleration", 0, "", "computed", 0)

    # We approximate acceleration from the magnitude of oi_roc
    # High absolute oi_roc = rapid change = institutional activity
    if abs(oi_roc) > 10:
        price_history = state.get("price_history", [])
        if len(price_history) < 3:
            return SignalResult("oi_acceleration", 0, "", "computed", 0)

        price_dir = price_history[-1] - price_history[-3]

        if oi_roc > 10 and price_dir > 0:
            return SignalResult("oi_acceleration", 0.5, f"OI accelerating +{oi_roc:.0f}% with price up — institutional buying", "computed", 0.5)
        elif oi_roc > 10 and price_dir < 0:
            return SignalResult("oi_acceleration", -0.5, f"OI accelerating +{oi_roc:.0f}% with price down — institutional shorting", "computed", 0.5)
        elif oi_roc < -10:
            return SignalResult("oi_acceleration", 0, f"OI declining rapidly {oi_roc:.0f}% — deleveraging", "computed", 0.3)

    return SignalResult("oi_acceleration", 0, "", "computed", 0)


@signal("cvd_momentum", "computed", "Slope of cumulative volume delta — buying pressure trend")
def cvd_momentum(coin: str, state: dict) -> SignalResult:
    """Not just current CVD but whether buying pressure is INCREASING or DECREASING."""
    cvd = state.get("cvd", 0)
    volume_delta = state.get("volume_delta", 0)

    if cvd == 0 and volume_delta == 0:
        return SignalResult("cvd_momentum", 0, "", "computed", 0)

    # volume_delta is the per-candle buy-sell difference
    # If CVD is positive AND volume_delta is positive, buying is SUSTAINED
    # If CVD is positive BUT volume_delta is negative, buying is FADING
    if cvd > 0 and volume_delta > 0:
        return SignalResult("cvd_momentum", 0.4, "Sustained buying — CVD rising", "computed", 0.4)
    elif cvd > 0 and volume_delta < 0:
        return SignalResult("cvd_momentum", -0.2, "Buying fading — CVD positive but delta negative", "computed", 0.3)
    elif cvd < 0 and volume_delta < 0:
        return SignalResult("cvd_momentum", -0.4, "Sustained selling — CVD falling", "computed", 0.4)
    elif cvd < 0 and volume_delta > 0:
        return SignalResult("cvd_momentum", 0.2, "Selling fading — CVD negative but delta positive", "computed", 0.3)

    return SignalResult("cvd_momentum", 0, "", "computed", 0)


@signal("price_oracle_gap", "computed", "Perp price vs oracle price divergence")
def price_oracle_gap(coin: str, state: dict) -> SignalResult:
    """When perp price deviates from oracle, funding mechanics force convergence."""
    price = state.get("price", state.get("close", 0))
    oracle = state.get("oracle_price", 0)

    if price == 0 or oracle == 0:
        return SignalResult("price_oracle_gap", 0, "", "computed", 0)

    gap_pct = (price - oracle) / oracle

    if gap_pct > 0.005:
        score = -min(0.6, gap_pct / 0.01)
        return SignalResult("price_oracle_gap", score,
            f"Perp {gap_pct*100:+.2f}% above oracle — convergence short",
            "computed", 0.5)
    elif gap_pct < -0.005:
        score = min(0.6, abs(gap_pct) / 0.01)
        return SignalResult("price_oracle_gap", score,
            f"Perp {gap_pct*100:+.2f}% below oracle — convergence long",
            "computed", 0.5)

    return SignalResult("price_oracle_gap", 0, "", "computed", 0)


@signal("predicted_actual_divergence", "computed", "Predicted funding diverging from actual — early signal")
def predicted_actual_divergence(coin: str, state: dict) -> SignalResult:
    """When predicted funding is about to flip but actual hasn't yet = early entry window."""
    predicted = state.get("predicted_funding", 0)
    actual = state.get("funding_rate", 0)

    if predicted == 0 and actual == 0:
        return SignalResult("predicted_actual_divergence", 0, "", "computed", 0)

    # Predicted flipping direction before actual = early signal
    if predicted > 0.00001 and actual < -0.00001:
        return SignalResult("predicted_actual_divergence", -0.4,
            "Predicted funding flipping positive (actual still negative) — early short",
            "computed", 0.5)
    elif predicted < -0.00001 and actual > 0.00001:
        return SignalResult("predicted_actual_divergence", 0.4,
            "Predicted funding flipping negative (actual still positive) — early long",
            "computed", 0.5)

    # Same direction but predicted is much more extreme
    if abs(predicted) > abs(actual) * 3 and abs(predicted) > 0.00001:
        if predicted > 0:
            return SignalResult("predicted_actual_divergence", -0.3,
                "Predicted funding accelerating positive — shorts will earn more",
                "computed", 0.4)
        else:
            return SignalResult("predicted_actual_divergence", 0.3,
                "Predicted funding accelerating negative — longs will earn more",
                "computed", 0.4)

    return SignalResult("predicted_actual_divergence", 0, "", "computed", 0)


@signal("volume_weighted_rsi", "computed", "RSI weighted by relative volume — high-volume moves matter more")
def volume_weighted_rsi(coin: str, state: dict) -> SignalResult:
    """Standard RSI treats all candles equally. This weights by volume so
    a big-volume candle at RSI 30 is more meaningful than a thin one at RSI 25."""
    price_history = state.get("price_history", [])
    volume_history = state.get("volume_history", [])

    if len(price_history) < 15 or len(volume_history) < 15:
        return SignalResult("volume_weighted_rsi", 0, "", "computed", 0)

    prices = price_history[-15:]
    volumes = volume_history[-15:]
    avg_vol = sum(volumes) / len(volumes) if sum(volumes) > 0 else 1

    # Volume-weighted gains and losses
    wt_gain = 0
    wt_loss = 0
    for i in range(1, len(prices)):
        if prices[i - 1] == 0:
            continue
        change = (prices[i] - prices[i - 1]) / prices[i - 1]
        weight = volumes[i] / avg_vol if avg_vol > 0 else 1
        if change > 0:
            wt_gain += change * weight
        else:
            wt_loss += abs(change) * weight

    if wt_loss == 0:
        vwrsi = 100
    else:
        rs = wt_gain / wt_loss
        vwrsi = 100 - (100 / (1 + rs))

    if vwrsi < 25:
        score = min(0.6, (30 - vwrsi) / 30)
        return SignalResult("volume_weighted_rsi", score,
            f"VWRSI {vwrsi:.0f} — oversold on heavy volume", "computed", 0.5)
    elif vwrsi > 75:
        score = -min(0.6, (vwrsi - 70) / 30)
        return SignalResult("volume_weighted_rsi", score,
            f"VWRSI {vwrsi:.0f} — overbought on heavy volume", "computed", 0.5)

    return SignalResult("volume_weighted_rsi", 0, "", "computed", 0)
