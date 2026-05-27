"""Realtime signals — from websocket trade tape, order flow, and vault positions.

These signals consume data from the WebSocket collector (rift_ws_collector.py)
and vault position polling (rift_data_collector.py). If data isn't available,
signals return 0 (dormant but ready).
"""

from rift_engine.signals.base import signal, SignalResult


@signal("trade_tape_imbalance", "realtime", "Buy/sell tape imbalance from real-time trade flow")
def trade_tape_imbalance(coin: str, state: dict) -> SignalResult:
    """Real-time trade-level buy/sell imbalance. More granular than CVD because
    it separates large trades (whale) from small trades (retail noise).

    When large trades are 80%+ one-sided, a whale is accumulating.
    When tape speed spikes, volatility expansion is imminent.
    """
    tape = state.get("tape", {})
    if not tape:
        return SignalResult("trade_tape_imbalance", 0, "", "realtime", 0)

    imbalance = tape.get("imbalance", 0)        # -1 to +1
    large_buy = tape.get("large_buy_volume", 0)
    large_sell = tape.get("large_sell_volume", 0)
    tape_speed = tape.get("tape_speed", 0)       # trades per minute
    total_volume = tape.get("total_volume", 0)

    if total_volume == 0:
        return SignalResult("trade_tape_imbalance", 0, "", "realtime", 0)

    # Large trade direction (whale signal)
    large_total = large_buy + large_sell
    if large_total > 0:
        large_imbalance = (large_buy - large_sell) / large_total
    else:
        large_imbalance = 0

    # Combine overall imbalance with large-trade imbalance (whale gets 2x weight)
    weighted_imbalance = (imbalance + large_imbalance * 2) / 3

    # Tape speed amplifier — high speed = more conviction
    speed_mult = min(1.5, max(0.5, tape_speed / 100))  # normalize around 100 trades/min

    if abs(weighted_imbalance) > 0.3:
        score = weighted_imbalance * 0.6 * speed_mult
        score = max(-0.8, min(0.8, score))

        if score > 0:
            reason = f"Tape bullish: imbalance {imbalance:+.2f}, large trades {large_imbalance:+.2f}, speed {tape_speed}/min"
        else:
            reason = f"Tape bearish: imbalance {imbalance:+.2f}, large trades {large_imbalance:+.2f}, speed {tape_speed}/min"

        confidence = min(0.7, abs(weighted_imbalance) * speed_mult)
        return SignalResult("trade_tape_imbalance", round(score, 3), reason, "realtime", round(confidence, 2))

    return SignalResult("trade_tape_imbalance", 0, "", "realtime", 0)


@signal("spoofing_detection", "realtime", "Phantom liquidity / order spoofing detection")
def spoofing_detection(coin: str, state: dict) -> SignalResult:
    """Detects spoofing by tracking order book depth that appears then vanishes
    before being filled. Large phantom bids = fake buy wall = bearish (they want
    you to think support exists). Large phantom asks = fake sell wall = bullish.

    The spoofer places large orders to manipulate perception, then cancels them.
    Trade AGAINST the phantom side — the spoofer is trying to exit the other way.
    """
    orderflow = state.get("orderflow", {})
    if not orderflow:
        return SignalResult("spoofing_detection", 0, "", "realtime", 0)

    phantom_bid = orderflow.get("phantom_bid_ratio", 0)
    phantom_ask = orderflow.get("phantom_ask_ratio", 0)
    bid_ratio = orderflow.get("bid_ratio", 0.5)
    avg_spread = orderflow.get("avg_spread_bps", 0)

    # Phantom ratio > 0.3 means 30%+ of depth on that side vanished without fills
    # This is suspicious — normal markets have <10% cancel rate on visible depth

    if phantom_bid > 0.3 and phantom_ask < 0.15:
        # Heavy phantom bids = fake buy support = bearish intent
        score = -min(0.6, phantom_bid * 1.5)
        return SignalResult("spoofing_detection", round(score, 3),
            f"Phantom bids detected ({phantom_bid:.0%} vanished) — fake support, bearish",
            "realtime", 0.5)

    elif phantom_ask > 0.3 and phantom_bid < 0.15:
        # Heavy phantom asks = fake sell wall = bullish intent
        score = min(0.6, phantom_ask * 1.5)
        return SignalResult("spoofing_detection", round(score, 3),
            f"Phantom asks detected ({phantom_ask:.0%} vanished) — fake resistance, bullish",
            "realtime", 0.5)

    elif phantom_bid > 0.25 and phantom_ask > 0.25:
        # Both sides spoofing — market is manipulated, reduce confidence in everything
        return SignalResult("spoofing_detection", 0,
            f"Bilateral spoofing (bid {phantom_bid:.0%}, ask {phantom_ask:.0%}) — reduce all signals",
            "realtime", 0.2)

    return SignalResult("spoofing_detection", 0, "", "realtime", 0)


@signal("vault_smart_money", "realtime", "Top vault position changes — institutional flow")
def vault_smart_money(coin: str, state: dict) -> SignalResult:
    """Tracks position changes in top Hyperliquid vaults (publicly visible).

    When multiple top vaults are accumulating the same coin, it's a strong
    directional signal — these are sophisticated operators with edge.
    When vaults are reducing, they're taking profit or cutting losses.
    """
    vaults = state.get("vault_positions", {})
    if not vaults:
        return SignalResult("vault_smart_money", 0, "", "realtime", 0)

    # vault_positions should be pre-aggregated per coin:
    # {num_long, num_short, net_notional, position_change_pct, total_vaults}
    num_long = vaults.get("num_long", 0)
    num_short = vaults.get("num_short", 0)
    total_vaults = vaults.get("total_vaults", 0)
    net_notional = vaults.get("net_notional", 0)
    position_change = vaults.get("position_change_pct", 0)

    if total_vaults == 0:
        return SignalResult("vault_smart_money", 0, "", "realtime", 0)

    long_pct = num_long / total_vaults if total_vaults > 0 else 0

    # Strong consensus — most vaults on same side
    if long_pct > 0.6 and num_long >= 3:
        score = min(0.7, long_pct * 0.8)
        # Boost if vaults are actively increasing
        if position_change > 5:
            score = min(0.8, score + 0.15)
        return SignalResult("vault_smart_money", round(score, 3),
            f"{num_long}/{total_vaults} vaults long {coin}, change {position_change:+.1f}%",
            "realtime", 0.6)

    elif long_pct < 0.4 and num_short >= 3:
        short_pct = 1 - long_pct
        score = -min(0.7, short_pct * 0.8)
        if position_change < -5:
            score = max(-0.8, score - 0.15)
        return SignalResult("vault_smart_money", round(score, 3),
            f"{num_short}/{total_vaults} vaults short {coin}, change {position_change:+.1f}%",
            "realtime", 0.6)

    # Mixed — no consensus
    if num_long > 0 and num_short > 0:
        return SignalResult("vault_smart_money", 0,
            f"Vaults split: {num_long} long, {num_short} short — no consensus",
            "realtime", 0.1)

    return SignalResult("vault_smart_money", 0, "", "realtime", 0)
