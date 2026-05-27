"""Microstructure signals — OI, volume flow, liquidation proximity, positioning, whale detection."""

import json
from pathlib import Path
from rift_engine.signals.base import signal, SignalResult


@signal("oi_divergence", "microstructure", "Open interest direction vs price direction")
def oi_divergence(coin: str, state: dict) -> SignalResult:
    oi_roc = state.get("oi_roc", 0)
    price_history = state.get("price_history", [])

    if oi_roc == 0 or len(price_history) < 5:
        return SignalResult("oi_divergence", 0, "", "microstructure", 0)

    # Price direction
    price_change = (price_history[-1] - price_history[-5]) / price_history[-5] if price_history[-5] != 0 else 0

    # Rising OI + rising price = trend confirmation (bullish)
    # Rising OI + falling price = short buildup (bearish — fragile)
    # Falling OI + rising price = short squeeze (bullish but weak)
    # Falling OI + falling price = long capitulation (bearish)
    if oi_roc > 5 and price_change > 0.01:
        return SignalResult("oi_divergence", 0.5, f"Rising OI + rising price — trend confirmed", "microstructure", 0.5)
    elif oi_roc > 5 and price_change < -0.01:
        return SignalResult("oi_divergence", -0.6, f"Rising OI + falling price — shorts building", "microstructure", 0.6)
    elif oi_roc < -5 and price_change > 0.01:
        return SignalResult("oi_divergence", 0.3, f"Falling OI + rising price — short squeeze", "microstructure", 0.4)
    elif oi_roc < -5 and price_change < -0.01:
        return SignalResult("oi_divergence", -0.4, f"Falling OI + falling price — capitulation", "microstructure", 0.5)

    return SignalResult("oi_divergence", 0, "", "microstructure", 0)


@signal("volume_imbalance", "microstructure", "Buy vs sell volume imbalance (CVD)")
def volume_imbalance(coin: str, state: dict) -> SignalResult:
    cvd = state.get("cvd", 0)
    relative_volume = state.get("relative_volume", 1.0)

    if cvd == 0:
        return SignalResult("volume_imbalance", 0, "", "microstructure", 0)

    # CVD positive = cumulative buying pressure, negative = selling pressure
    # More meaningful when volume is above average
    vol_mult = min(1.5, max(0.5, relative_volume))

    if cvd > 0:
        score = min(0.6, 0.3 * vol_mult)
        return SignalResult("volume_imbalance", score, f"Buyers dominating (CVD positive, rvol {relative_volume:.1f}x)", "microstructure", 0.4)
    else:
        score = -min(0.6, 0.3 * vol_mult)
        return SignalResult("volume_imbalance", score, f"Sellers dominating (CVD negative, rvol {relative_volume:.1f}x)", "microstructure", 0.4)


@signal("volume_surge", "microstructure", "Abnormally high volume — institutional activity")
def volume_surge(coin: str, state: dict) -> SignalResult:
    relative_volume = state.get("relative_volume", 1.0)

    if relative_volume < 1.5:
        return SignalResult("volume_surge", 0, "", "microstructure", 0)

    # Volume surge doesn't tell direction — but it confirms conviction
    # Score direction from price action
    price_history = state.get("price_history", [])
    if len(price_history) < 3:
        return SignalResult("volume_surge", 0, "", "microstructure", 0)

    price_dir = price_history[-1] - price_history[-3]

    if relative_volume > 3.0:
        score = 0.7 if price_dir > 0 else -0.7
        return SignalResult("volume_surge", score, f"Volume spike {relative_volume:.1f}x — whale activity", "microstructure", 0.7)
    elif relative_volume > 2.0:
        score = 0.5 if price_dir > 0 else -0.5
        return SignalResult("volume_surge", score, f"High volume {relative_volume:.1f}x", "microstructure", 0.5)
    else:
        score = 0.2 if price_dir > 0 else -0.2
        return SignalResult("volume_surge", score, f"Above average volume {relative_volume:.1f}x", "microstructure", 0.3)


@signal("oi_zscore", "microstructure", "Open interest z-score — extreme positioning")
def oi_zscore_signal(coin: str, state: dict) -> SignalResult:
    zscore = state.get("oi_zscore", 0)

    if abs(zscore) < 1.0:
        return SignalResult("oi_zscore", 0, "", "microstructure", 0)

    # Extreme OI = crowded market = fragile = mean reversion likely
    if zscore > 2.0:
        score = -min(0.7, zscore / 4.0)
        return SignalResult("oi_zscore", score, f"OI extremely high (z={zscore:.1f}) — overcrowded", "microstructure", 0.5)
    elif zscore < -2.0:
        score = min(0.7, abs(zscore) / 4.0)
        return SignalResult("oi_zscore", score, f"OI extremely low (z={zscore:.1f}) — washed out", "microstructure", 0.5)

    return SignalResult("oi_zscore", 0, "", "microstructure", 0)


@signal("net_positioning", "microstructure", "Net long/short positioning delta")
def net_positioning(coin: str, state: dict) -> SignalResult:
    net_delta = state.get("net_delta", 0)

    if net_delta == 0:
        return SignalResult("net_positioning", 0, "", "microstructure", 0)

    # Extreme net positioning = crowded = reversion signal
    if net_delta > 0:
        # Net long heavy — contrarian short
        score = -min(0.5, net_delta / 1000)
        return SignalResult("net_positioning", score, f"Net longs dominating — crowded long", "microstructure", 0.4)
    else:
        # Net short heavy — contrarian long
        score = min(0.5, abs(net_delta) / 1000)
        return SignalResult("net_positioning", score, f"Net shorts dominating — crowded short", "microstructure", 0.4)


@signal("liquidation_proximity", "microstructure", "Price near estimated liquidation clusters — cascade risk")
def liquidation_proximity(coin: str, state: dict) -> SignalResult:
    """Detect when OI is high + funding extreme + price stretched = liquidation cascade likely.

    High OI + extreme funding = overleveraged market. When price moves against
    the crowded side, liquidations cascade and price overshoots. Position for
    the reversion after the cascade.
    """
    oi_zscore = state.get("oi_zscore", 0)
    funding = state.get("funding_rate", 0)
    premium = state.get("premium", 0)

    if abs(oi_zscore) < 1.0:
        return SignalResult("liquidation_proximity", 0, "", "microstructure", 0)

    # High OI + positive funding + positive premium = longs overcrowded
    # A price drop will trigger long liquidations → cascade → overshoot → reversion long
    if oi_zscore > 1.5 and funding > 0.00001 and premium > 0.001:
        score = 0.5  # contrarian long — expect cascade then reversion
        return SignalResult("liquidation_proximity", score,
            f"Long liquidation risk building (OI z={oi_zscore:.1f}, funding +) — reversion after cascade",
            "microstructure", 0.5)

    # High OI + negative funding + negative premium = shorts overcrowded
    if oi_zscore > 1.5 and funding < -0.00001 and premium < -0.001:
        score = -0.5  # contrarian short — expect short squeeze then reversion
        return SignalResult("liquidation_proximity", score,
            f"Short squeeze risk (OI z={oi_zscore:.1f}, funding -) — reversion after cascade",
            "microstructure", 0.5)

    return SignalResult("liquidation_proximity", 0, "", "microstructure", 0)


@signal("whale_activity", "microstructure", "Abnormal large trade detection")
def whale_activity(coin: str, state: dict) -> SignalResult:
    """Detect whale activity from volume delta and volume surge.

    When volume is >3x average AND volume delta is strongly directional,
    a large player is moving. Short-term price follows the whale.
    """
    relative_volume = state.get("relative_volume", 1.0)
    volume_delta = state.get("volume_delta", 0)

    if relative_volume < 2.5 or volume_delta == 0:
        return SignalResult("whale_activity", 0, "", "microstructure", 0)

    # Very high volume + strong directional delta = whale buying/selling
    if relative_volume > 3.0:
        if volume_delta > 0:
            return SignalResult("whale_activity", 0.6,
                f"Whale buying detected (vol {relative_volume:.1f}x, delta positive)",
                "microstructure", 0.6)
        else:
            return SignalResult("whale_activity", -0.6,
                f"Whale selling detected (vol {relative_volume:.1f}x, delta negative)",
                "microstructure", 0.6)
    else:
        if volume_delta > 0:
            return SignalResult("whale_activity", 0.3,
                f"Large buyer (vol {relative_volume:.1f}x)",
                "microstructure", 0.4)
        else:
            return SignalResult("whale_activity", -0.3,
                f"Large seller (vol {relative_volume:.1f}x)",
                "microstructure", 0.4)


@signal("orderbook_imbalance", "microstructure", "Bid/ask depth imbalance predicts short-term direction")
def orderbook_imbalance(coin: str, state: dict) -> SignalResult:
    """Read order book imbalance from collected snapshot data.

    A 70/30 bid-heavy book predicts upward pressure.
    A 30/70 ask-heavy book predicts downward pressure.
    """
    # Order book data comes from our collector if available
    bids_depth = state.get("bids_depth", 0)
    asks_depth = state.get("asks_depth", 0)

    if bids_depth == 0 and asks_depth == 0:
        return SignalResult("orderbook_imbalance", 0, "", "microstructure", 0)

    total = bids_depth + asks_depth
    if total == 0:
        return SignalResult("orderbook_imbalance", 0, "", "microstructure", 0)

    bid_ratio = bids_depth / total

    if bid_ratio > 0.65:
        score = min(0.5, (bid_ratio - 0.5) * 2)
        return SignalResult("orderbook_imbalance", score,
            f"Bid-heavy book ({bid_ratio:.0%}) — buying pressure",
            "microstructure", 0.5)
    elif bid_ratio < 0.35:
        score = -min(0.5, (0.5 - bid_ratio) * 2)
        return SignalResult("orderbook_imbalance", score,
            f"Ask-heavy book ({bid_ratio:.0%}) — selling pressure",
            "microstructure", 0.5)

    return SignalResult("orderbook_imbalance", 0, "", "microstructure", 0)
