"""Seasonality signals — time-based patterns around funding and session transitions."""

import time
from datetime import datetime, timezone
from rift_engine.signals.base import signal, SignalResult


@signal("funding_settlement_window", "seasonality", "Approaching hourly funding settlement")
def funding_settlement_window(coin: str, state: dict) -> SignalResult:
    now = time.time()
    minutes_to_hour = 60 - (int(now) % 3600) // 60

    funding_rate = state.get("funding_rate", 0)

    if minutes_to_hour > 30 or abs(funding_rate) < 0.000005:
        return SignalResult("funding_settlement_window", 0, "", "seasonality", 0)

    if minutes_to_hour <= 15:
        if funding_rate > 0.00001:
            return SignalResult("funding_settlement_window", -0.3, f"Pre-settlement drift — longs reducing ({minutes_to_hour}m to settle)", "seasonality", 0.4)
        elif funding_rate < -0.00001:
            return SignalResult("funding_settlement_window", 0.3, f"Pre-settlement drift — shorts reducing ({minutes_to_hour}m to settle)", "seasonality", 0.4)
    elif minutes_to_hour <= 30:
        if funding_rate > 0.00002:
            return SignalResult("funding_settlement_window", -0.15, f"Approaching settlement — funding positive", "seasonality", 0.2)
        elif funding_rate < -0.00002:
            return SignalResult("funding_settlement_window", 0.15, f"Approaching settlement — funding negative", "seasonality", 0.2)

    return SignalResult("funding_settlement_window", 0, "", "seasonality", 0)


@signal("session_transition", "seasonality", "Trading session transitions — Asia/EU/US")
def session_transition(coin: str, state: dict) -> SignalResult:
    now = datetime.now(timezone.utc)
    hour = now.hour

    # Key session boundaries (UTC):
    # Asia open: 00:00-01:00 (often low vol, range-bound)
    # EU open: 07:00-08:00 (vol pickup, trend initiation)
    # US open: 13:00-14:00 (highest vol, strongest moves)
    # US close: 20:00-21:00 (vol decline, position squaring)

    price_history = state.get("price_history", [])
    if len(price_history) < 5:
        return SignalResult("session_transition", 0, "", "seasonality", 0)

    recent_dir = price_history[-1] - price_history[-3]

    if hour in (13, 14):
        # US open — strongest session, trend continuation likely
        if recent_dir > 0:
            return SignalResult("session_transition", 0.3, "US session open — bullish momentum likely to accelerate", "seasonality", 0.4)
        elif recent_dir < 0:
            return SignalResult("session_transition", -0.3, "US session open — bearish momentum likely to accelerate", "seasonality", 0.4)
    elif hour in (7, 8):
        # EU open — vol pickup
        if recent_dir > 0:
            return SignalResult("session_transition", 0.2, "EU session open — trend may continue", "seasonality", 0.3)
        elif recent_dir < 0:
            return SignalResult("session_transition", -0.2, "EU session open — trend may continue", "seasonality", 0.3)
    elif hour in (0, 1):
        # Asia session — often mean-reverting
        if recent_dir > 0:
            return SignalResult("session_transition", -0.15, "Asia session — prior move may revert", "seasonality", 0.2)
        elif recent_dir < 0:
            return SignalResult("session_transition", 0.15, "Asia session — prior move may revert", "seasonality", 0.2)

    return SignalResult("session_transition", 0, "", "seasonality", 0)


@signal("new_listing_spike", "seasonality", "Recently listed coin — extreme vol and momentum in first 72h")
def new_listing_spike(coin: str, state: dict) -> SignalResult:
    """New perp listings on Hyperliquid get massive volume spikes in the first 24-72h.
    Funding is usually wildly positive (retail rushes to long), OI builds fast,
    and price whipsaws. Trade the mean reversion after the initial spike."""
    listing_age_hours = state.get("listing_age_hours", None)

    if listing_age_hours is None or listing_age_hours > 72:
        return SignalResult("new_listing_spike", 0, "", "seasonality", 0)

    funding_rate = state.get("funding_rate", 0)
    relative_volume = state.get("relative_volume", 1.0)

    # First 24 hours — extreme volatility, fade the crowd
    if listing_age_hours <= 24:
        if funding_rate > 0.0001:
            return SignalResult("new_listing_spike", -0.7,
                f"New listing ({listing_age_hours:.0f}h old), funding {funding_rate*100:.3f}% — fade retail longs",
                "seasonality", 0.6)
        elif funding_rate < -0.0001:
            return SignalResult("new_listing_spike", 0.7,
                f"New listing ({listing_age_hours:.0f}h old), funding {funding_rate*100:.3f}% — fade retail shorts",
                "seasonality", 0.6)
        elif relative_volume > 5.0:
            return SignalResult("new_listing_spike", 0,
                f"New listing ({listing_age_hours:.0f}h old), vol {relative_volume:.0f}x — too chaotic",
                "seasonality", 0.1)

    # 24-72 hours — still elevated but more predictable
    elif listing_age_hours <= 72:
        if funding_rate > 0.00005:
            return SignalResult("new_listing_spike", -0.4,
                f"Recent listing ({listing_age_hours:.0f}h), funding still elevated — mild short bias",
                "seasonality", 0.4)
        elif funding_rate < -0.00005:
            return SignalResult("new_listing_spike", 0.4,
                f"Recent listing ({listing_age_hours:.0f}h), negative funding — mild long bias",
                "seasonality", 0.4)

    return SignalResult("new_listing_spike", 0, "", "seasonality", 0)
