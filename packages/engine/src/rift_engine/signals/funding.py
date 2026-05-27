"""Funding rate signals — alpha from Hyperliquid's hourly settlement mechanics."""

from rift_engine.signals.base import signal, SignalResult


@signal("funding_extreme", "funding", "Extreme predicted funding rate — shorts/longs overcrowded")
def funding_extreme(coin: str, state: dict) -> SignalResult:
    predicted = state.get("predicted_funding", 0)
    if predicted == 0:
        return SignalResult("funding_extreme", 0, "", "funding", 0)

    # Positive funding = longs paying → short signal (shorts will collect)
    # Negative funding = shorts paying → long signal (longs will collect)
    threshold = 0.000015

    if predicted > threshold * 2:
        score = -min(1.0, predicted / 0.0001)  # scale to -1
        return SignalResult("funding_extreme", score, f"Funding +{predicted*100:.4f}% — longs overcrowded", "funding", 0.7)
    elif predicted < -threshold * 2:
        score = min(1.0, abs(predicted) / 0.0001)
        return SignalResult("funding_extreme", score, f"Funding {predicted*100:.4f}% — shorts overcrowded", "funding", 0.7)
    elif predicted > threshold:
        score = -0.3
        return SignalResult("funding_extreme", score, f"Funding mildly positive +{predicted*100:.4f}%", "funding", 0.4)
    elif predicted < -threshold:
        score = 0.3
        return SignalResult("funding_extreme", score, f"Funding mildly negative {predicted*100:.4f}%", "funding", 0.4)

    return SignalResult("funding_extreme", 0, "", "funding", 0)


@signal("funding_divergence", "funding", "Hyperliquid funding diverges from CEX average")
def funding_divergence(coin: str, state: dict) -> SignalResult:
    div = state.get("funding_divergence", 0)
    if abs(div) < 0.0002:
        return SignalResult("funding_divergence", 0, "", "funding", 0)

    # HL funding higher than CEX → short on HL (arb opportunity)
    if div > 0.0005:
        score = -min(1.0, div / 0.002)
        return SignalResult("funding_divergence", score, f"HL funding {div*100:+.3f}% above CEX", "funding", 0.5)
    elif div < -0.0005:
        score = min(1.0, abs(div) / 0.002)
        return SignalResult("funding_divergence", score, f"HL funding {div*100:+.3f}% below CEX", "funding", 0.5)
    elif div > 0.0002:
        return SignalResult("funding_divergence", -0.2, f"HL slightly above CEX", "funding", 0.3)
    else:
        return SignalResult("funding_divergence", 0.2, f"HL slightly below CEX", "funding", 0.3)


@signal("funding_zscore", "funding", "Funding rate z-score vs 7-day rolling window")
def funding_zscore(coin: str, state: dict) -> SignalResult:
    zscore = state.get("funding_rate_zscore", 0)
    if abs(zscore) < 1.0:
        return SignalResult("funding_zscore", 0, "", "funding", 0)

    if zscore > 2.0:
        score = -min(1.0, zscore / 4.0)
        return SignalResult("funding_zscore", score, f"Funding z-score {zscore:.1f} — extremely high", "funding", 0.6)
    elif zscore < -2.0:
        score = min(1.0, abs(zscore) / 4.0)
        return SignalResult("funding_zscore", score, f"Funding z-score {zscore:.1f} — extremely low", "funding", 0.6)
    elif zscore > 1.0:
        return SignalResult("funding_zscore", -0.2, f"Funding elevated (z={zscore:.1f})", "funding", 0.3)
    else:
        return SignalResult("funding_zscore", 0.2, f"Funding depressed (z={zscore:.1f})", "funding", 0.3)
