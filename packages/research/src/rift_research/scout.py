"""Scout — multi-timeframe market scanner using the signal factory.

Two-phase scan:
  Phase 1 (Bias): Scan 1h candles for directional bias. Only coins with
    strong multi-category agreement and funding alignment pass.
  Phase 2 (Entry): For coins that pass, scan 5m candles for an entry
    setup that aligns with the bias. A 5m oversold dip in a 1h uptrend
    is a high-probability entry.

The combined score (bias strength × entry quality) determines the final
ranking. Recon takes the top pick and executes.

Output is NDJSON for consumption by the CLI, webapp, and Recon mode.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

import numpy as np

from rift_engine.signals.aggregator import aggregate_signals, Opportunity
from rift_substrate.universe import Universe, UniverseSpec


# ── Validated strategy/coin edge map ──────────────────────────
# Auto-loaded from ~/.rift/validated_edge.json (written by rift portfolio-matrix).
# Falls back to hardcoded defaults if cache is missing or > 30 days old.
# Empty by default. Run 'rift portfolio-matrix' to populate ~/.rift/validated_edge.json
_DEFAULT_VALIDATED_EDGE: dict[str, dict[str, dict]] = {}
# Coins that lost on all tested strategies. Auto-populated by 'rift portfolio-matrix'.
_DEFAULT_BLACKLISTED: set[str] = set()


def _load_validated_edge() -> tuple[dict, set, set]:
    """Load validated edge from cache file, fall back to hardcoded defaults."""
    from pathlib import Path
    import json as _json
    import time as _time

    cache_path = Path.home() / ".rift" / "validated_edge.json"
    if cache_path.exists():
        try:
            data = _json.loads(cache_path.read_text())
            # Check age — reject if > 30 days old
            generated = data.get("generated", "")
            if generated:
                from datetime import datetime
                gen_time = datetime.fromisoformat(generated)
                age_days = (_time.time() - gen_time.timestamp()) / 86400
                if age_days > 30:
                    return _DEFAULT_VALIDATED_EDGE, set(_DEFAULT_BLACKLISTED), set()

            edge = data.get("validated_edge", _DEFAULT_VALIDATED_EDGE)
            blacklisted = set(data.get("blacklisted_coins", _DEFAULT_BLACKLISTED))
            validated = set(data.get("validated_coins", []))
            return edge, blacklisted, validated
        except Exception:
            pass

    # Fall back to defaults
    validated = set()
    for edges in _DEFAULT_VALIDATED_EDGE.values():
        validated.update(edges.keys())
    return _DEFAULT_VALIDATED_EDGE, _DEFAULT_BLACKLISTED, validated


VALIDATED_EDGE, BLACKLISTED_COINS, VALIDATED_COINS = _load_validated_edge()


def _coin_has_edge(coin: str) -> bool:
    """Check if any validated strategy is profitable on this coin."""
    return coin in VALIDATED_COINS


def _get_coin_best_edge(coin: str) -> dict | None:
    """Get the best validated strategy/edge for a coin, or None."""
    best = None
    best_score = -999
    for strat, edges in VALIDATED_EDGE.items():
        if coin in edges:
            # Score by months_positive (HF) or sharpe (legacy)
            score = edges[coin].get("months_positive", edges[coin].get("sharpe", 0))
            if score > best_score:
                best_score = score
                best = {"strategy": strat, **edges[coin]}
    return best


def _emit(data: dict) -> None:
    print(json.dumps(data), flush=True)


def scan_market(
    top_n: int = 20,
    bias_tf: str = "1h",
    entry_tf: str = "5m",
    min_score: float = 0.15,
    min_signals: int = 2,
    min_confluence: int | None = None,  # CLI compat
    soak_seconds: int = 120,  # 0 = no soak
    # Legacy single-TF mode (if caller passes interval= instead of bias_tf=)
    interval: str | None = None,
) -> list[Opportunity]:
    """Scan top coins with two-phase bias + entry detection.

    Args:
        top_n: Number of top coins by 24h volume to scan
        bias_tf: Higher timeframe for directional bias (default "1h")
        entry_tf: Lower timeframe for entry timing (default "5m")
        min_score: Minimum combined score to include
        min_signals: Minimum signals on bias timeframe
        min_confluence: Alias for min_signals (CLI compat)
        soak_seconds: Seconds to collect live websocket data before scanning (0 = skip)
        interval: Legacy alias for bias_tf

    Returns sorted list of Opportunities (best first).
    """
    if min_confluence is not None:
        min_signals = min_confluence
    if interval is not None:
        bias_tf = interval

    from rift_data.data import (
        get_info_client, fetch_predicted_funding,
        fetch_market_context, fetch_cross_exchange_funding, fetch_market_breadth,
    )
    from rift_research.signal_memory import get_hit_rate

    info = get_info_client()

    # Get all mids and HL meta+asset_ctxs (single roundtrip)
    all_mids = info.all_mids()
    try:
        meta_response = info.meta_and_asset_ctxs()
        meta = meta_response[0]
        asset_ctxs = meta_response[1] if len(meta_response) > 1 else []
    except Exception:
        _emit({"type": "error", "msg": "Cannot connect to Hyperliquid API"})
        return []

    # Universe selection — delegate to substrate (single source of truth).
    # 100k volume floor preserves prior behaviour. Blacklist comes from
    # the user's validated_edge.json (empty by default).
    spec = Universe.from_hl_data(
        meta=meta,
        asset_ctxs=asset_ctxs,
        min_volume_24h_usd=100_000,
        exclude=list(BLACKLISTED_COINS),
    )
    selected = spec.top_by_volume(top_n)

    # Build coin → live ctx dict for downstream per-coin lookups (funding,
    # open interest, oracle price, premium). Same HL data, just indexed.
    ctx_by_coin: dict[str, dict] = {}
    for i, asset in enumerate(meta.get("universe", [])):
        coin_name = str(asset["name"]).upper()
        if i < len(asset_ctxs):
            ctx_by_coin[coin_name] = asset_ctxs[i]

    # Re-shape into the (coin, day_vol, ctx) tuples the rest of the function
    # expects, ordered by volume descending.
    top_coins: list[tuple[str, float, dict]] = []
    for coin in selected.coins:
        ctx = ctx_by_coin.get(coin, {})
        day_vol = float(spec.metadata[coin].avg_volume_24h_usd or 0.0)
        top_coins.append((coin, day_vol, ctx))
    top_coins.sort(key=lambda x: x[1], reverse=True)

    # Shared context (fetched once)
    breadth = fetch_market_breadth(info)
    breadth_ob = breadth.get("overbought_pct", 0) if breadth else 0
    breadth_os = breadth.get("oversold_pct", 0) if breadth else 0
    market_avg_rsi = breadth.get("avg_rsi", 50) if breadth else 50

    # BTC momentum for btc_lead_lag signal
    btc_momentum = 0
    try:
        end_time = int(time.time() * 1000)
        btc_candles = info.candles_snapshot("BTC", bias_tf, end_time - 24 * 3600 * 1000, end_time)
        if btc_candles and len(btc_candles) >= 5:
            btc_closes = [float(c["c"]) for c in btc_candles]
            btc_momentum = (btc_closes[-1] - btc_closes[-3]) / btc_closes[-3] if btc_closes[-3] > 0 else 0
    except Exception:
        pass

    # ══════════════════════════════════════════════════════
    #  SOAK PHASE: Collect live websocket data
    # ══════════════════════════════════════════════════════
    soak_data: dict[str, dict] = {}
    coin_names = [c[0] for c in top_coins]

    if soak_seconds > 0:
        from rift_data.ws_feed import MultiCoinFeed

        _emit({"type": "status", "msg": f"Soaking live data for {soak_seconds}s across {len(coin_names)} coins..."})
        feed = MultiCoinFeed(coin_names)
        feed.start()

        def _soak_progress(elapsed, total, trades):
            if elapsed % 10 == 0:  # emit every 10 seconds
                _emit({"type": "soak", "elapsed": elapsed, "total": total, "trades": trades})

        feed.soak(soak_seconds, progress_cb=_soak_progress)
        soak_data = feed.get_all_derived()
        feed.stop()

        _emit({"type": "status", "msg": f"Soak complete: {feed.total_trades:,} trades collected"})

    opportunities: list[Opportunity] = []

    for idx, (coin, day_vol, ctx) in enumerate(top_coins):
        _emit({"type": "progress", "coin": coin, "pct": round((idx + 1) / len(top_coins) * 100), "phase": "bias"})

        mid_price = float(all_mids.get(coin, "0"))
        if mid_price <= 0:
            continue

        # Market context — prefer live soak data over REST snapshot
        coin_soak = soak_data.get(coin, {})
        soak_ctx = coin_soak  # may be empty dict if no soak

        # If soak captured live activeAssetCtx, use it (more current than REST)
        if soak_ctx.get("funding_rate"):
            funding_rate = soak_ctx["funding_rate"]
            open_interest = soak_ctx.get("open_interest", 0)
            oracle_price = soak_ctx.get("oracle_price", mid_price)
        else:
            funding_rate = float(ctx.get("funding", "0"))
            open_interest = float(ctx.get("openInterest", "0"))
            oracle_price = float(ctx.get("oraclePrice", mid_price))

        premium = float(ctx.get("premium", "0"))
        predicted_funding = fetch_predicted_funding(coin, info)

        cross_funding = fetch_cross_exchange_funding(coin, info)
        funding_divergence = cross_funding.get("hl_vs_cex", 0.0) if cross_funding else 0.0

        # ══════════════════════════════════════════════════════
        #  PHASE 1: BIAS DETECTION (higher timeframe)
        # ══════════════════════════════════════════════════════
        try:
            end_time = int(time.time() * 1000)
            bias_candles = info.candles_snapshot(coin, bias_tf, end_time - 120 * 3600 * 1000, end_time)
            if not bias_candles or len(bias_candles) < 20:
                continue
        except Exception:
            continue

        bias_state = _build_state(
            bias_candles, mid_price, funding_rate, predicted_funding,
            premium, open_interest, oracle_price, day_vol,
            funding_divergence, breadth_ob, breadth_os, market_avg_rsi,
            btc_momentum, soak_data=coin_soak,
        )

        bias_opp = aggregate_signals(coin, bias_state)
        if bias_opp is None or bias_opp.num_signals < min_signals:
            continue

        if bias_opp.score < min_score:
            continue

        # ─── BIAS QUALITY FILTERS ───

        # Filter 1: Category diversity — at least 3 independent categories agree
        agreeing_categories = set()
        for s in bias_opp.signals:
            if (s["score"] > 0) == (bias_opp.raw_score > 0):
                agreeing_categories.add(s["category"])
        if len(agreeing_categories) < 3:
            continue

        # Filter 2: Funding alignment — never fight funding
        if bias_opp.direction == "LONG" and funding_rate > 0.0002:
            continue
        if bias_opp.direction == "SHORT" and funding_rate < -0.0002:
            continue

        # Filter 3: Volume floor — require meaningful 24h volume
        # Don't use relative_volume (compares partial candle to completed = always low)
        # Instead check that the coin has real liquidity via 24h notional volume
        if day_vol < 1_000_000:  # $1M minimum 24h volume
            continue

        # Filter 4: Signal memory kill switch
        bias_signal_names = [s["name"] for s in bias_opp.signals]
        hit_rate = get_hit_rate(coin, bias_opp.direction.lower(), bias_signal_names)
        if hit_rate is not None and hit_rate < 0.45:
            continue

        bias_direction = bias_opp.direction  # "LONG" or "SHORT"
        bias_score = bias_opp.score

        _emit({"type": "progress", "coin": coin, "pct": round((idx + 1) / len(top_coins) * 100),
               "phase": "entry", "bias": bias_direction, "bias_score": round(bias_score, 3)})

        # ══════════════════════════════════════════════════════
        #  PHASE 2: ENTRY TIMING (lower timeframe)
        # ══════════════════════════════════════════════════════
        try:
            end_time = int(time.time() * 1000)
            entry_candles = info.candles_snapshot(coin, entry_tf, end_time - 12 * 3600 * 1000, end_time)
            if not entry_candles or len(entry_candles) < 20:
                # No entry data — still report the bias but with lower score
                entry_score = 0.0
                entry_signals = []
                entry_categories = set()
                has_entry = False
            else:
                has_entry = True
        except Exception:
            entry_score = 0.0
            entry_signals = []
            entry_categories = set()
            has_entry = False

        if has_entry:
            entry_state = _build_state(
                entry_candles, mid_price, funding_rate, predicted_funding,
                premium, open_interest, oracle_price, day_vol,
                funding_divergence, breadth_ob, breadth_os, market_avg_rsi,
                btc_momentum, soak_data=coin_soak,
            )

            entry_opp = aggregate_signals(coin, entry_state)

            if entry_opp is not None and entry_opp.direction == bias_direction:
                # Entry aligns with bias — strong setup
                entry_score = entry_opp.score
                entry_signals = entry_opp.signals
                entry_categories = set(s["category"] for s in entry_opp.signals
                                       if (s["score"] > 0) == (entry_opp.raw_score > 0))
            elif entry_opp is not None and entry_opp.direction != bias_direction:
                # Entry OPPOSES bias — lower timeframe is pulling back
                # This is actually an entry opportunity if it's a pullback, not a reversal
                # A mild counter-signal on 5m during strong 1h trend = buy the dip
                if entry_opp.score < bias_score * 0.5:
                    # Weak opposition — this is a pullback, good entry
                    entry_score = 0.3  # bonus for catching a dip
                    entry_signals = entry_opp.signals
                    entry_categories = set()
                else:
                    # Strong opposition — 5m is fighting 1h, skip
                    continue
            else:
                entry_score = 0.0
                entry_signals = []
                entry_categories = set()

        # ══════════════════════════════════════════════════════
        #  COMBINED SCORE
        # ══════════════════════════════════════════════════════
        # Bias is 60% of the score, entry timing is 40%
        combined_score = bias_score * 0.6 + entry_score * 0.4

        # Boost for hit rate
        if hit_rate is not None and hit_rate > 0.6:
            combined_score = min(1.0, combined_score + 0.10)

        # Boost/penalize based on validated backtest edge
        coin_edge = _get_coin_best_edge(coin)
        if coin_edge is not None:
            # Validated profitable pair — boost by monthly consistency
            months_pct = coin_edge.get("months_positive", coin_edge.get("sharpe", 50))
            edge_boost = min(0.15, (months_pct / 100) * 0.15)
            combined_score = min(1.0, combined_score + edge_boost)
        elif coin not in VALIDATED_COINS:
            # Unvalidated coin — penalize score (no proven edge)
            combined_score *= 0.7

        # Compute ATR-based entry/stop/target from the entry timeframe
        if has_entry:
            entry_closes = [float(c["c"]) for c in entry_candles]
            entry_highs = [float(c["h"]) for c in entry_candles]
            entry_lows = [float(c["l"]) for c in entry_candles]
            entry_atr = _compute_atr(entry_highs, entry_lows, entry_closes, 14)
        else:
            bias_closes = [float(c["c"]) for c in bias_candles]
            bias_highs = [float(c["h"]) for c in bias_candles]
            bias_lows = [float(c["l"]) for c in bias_candles]
            entry_atr = _compute_atr(bias_highs, bias_lows, bias_closes, 14)

        atr_val = float(entry_atr) if entry_atr > 0 else mid_price * 0.01
        atr_pct = atr_val / mid_price

        stop_dist = atr_val * 2
        if bias_direction == "LONG":
            entry_price = round(mid_price, 6)
            stop_price = round(mid_price - stop_dist, 6)
            target_price = round(mid_price + stop_dist * 2, 6)
        else:
            entry_price = round(mid_price, 6)
            stop_price = round(mid_price + stop_dist, 6)
            target_price = round(mid_price - stop_dist * 2, 6)

        rr = round(abs(target_price - mid_price) / stop_dist, 1) if stop_dist > 0 else 0

        # Merge signals from both phases
        all_signals = bias_opp.signals + [
            {**s, "name": f"{s['name']}_entry"} for s in entry_signals
            if s["name"] not in [bs["name"] for bs in bias_opp.signals]
        ]
        all_signal_names = [s["name"] for s in all_signals]
        all_categories = list(agreeing_categories | entry_categories)

        # ══════════════════════════════════════════════════════
        #  MISSION BRIEF — sizing, leverage, hold, confidence
        # ══════════════════════════════════════════════════════

        # Hold type — derived from which signal categories dominate.
        # Style-agnostic: any quant style is welcome; we just pick the right
        # exit profile from what the signals are telling us.
        cat_counts = {}
        for s in all_signals:
            if (s["score"] > 0) == (bias_opp.raw_score > 0):
                cat_counts[s["category"]] = cat_counts.get(s["category"], 0) + 1

        funding_cats = cat_counts.get("funding", 0) + cat_counts.get("seasonality", 0)
        vol_cats = cat_counts.get("volatility", 0)

        if funding_cats >= 2:
            hold_type = "funding"
        elif vol_cats >= 2:
            hold_type = "mean_reversion"
        else:
            hold_type = "momentum"

        # Staleness = ~2 bias-TF bars, capped [3 min, 4 hours]. Opportunity
        # freshness scales with the timeframe it was scouted on, not the
        # strategy style. Realtime signals halve the window.
        from rift_substrate import periods_per_year_for_interval
        bias_tf_minutes = (365.0 * 24 * 60) / periods_per_year_for_interval(bias_tf)
        staleness = max(3, min(240, int(2 * bias_tf_minutes)))
        if any(s["category"] == "realtime" for s in all_signals):
            staleness = max(3, staleness // 2)

        # Confidence tier
        num_agree_cats = len(agreeing_categories)
        if num_agree_cats >= 5 and hit_rate is not None and hit_rate > 0.60:
            confidence = "high"
        elif num_agree_cats >= 4:
            confidence = "medium"
        else:
            confidence = "low"

        # Leverage (capped at 3x for single-shot trades)
        if confidence == "high" and combined_score >= 0.5:
            lev = 3
        elif confidence == "medium" and combined_score >= 0.35:
            lev = 2
        else:
            lev = 1

        # Position size — Kelly from signal memory × confluence multiplier
        from rift_research.signal_memory import get_kelly_sizing
        kelly = get_kelly_sizing(coin, bias_direction.lower(), all_signal_names)
        base_risk = kelly["risk_pct"] if kelly else 0.02
        size_pct = base_risk * (0.5 + bias_opp.confluence)
        size_pct = max(0.005, min(0.05, size_pct))

        opp = Opportunity(
            coin=coin,
            direction=bias_direction,
            score=round(combined_score, 3),
            raw_score=bias_opp.raw_score,
            signals=all_signals,
            num_signals=len(all_signals),
            num_agreeing=bias_opp.num_agreeing,
            confluence=bias_opp.confluence,
            categories=all_categories,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            risk_reward=rr,
            funding_rate=funding_rate,
            predicted_funding=predicted_funding,
            volume_24h=round(day_vol, 0),
            atr_pct=round(atr_pct * 100, 2),
            hit_rate=round(hit_rate, 3) if hit_rate is not None else None,
            signal_names=all_signal_names,
            num_categories=num_agree_cats,
            leverage=lev,
            size_pct=round(size_pct, 4),
            hold_type=hold_type,
            staleness_minutes=staleness,
            confidence_tier=confidence,
            validated_strategy=coin_edge["strategy"] if coin_edge else None,
            validated_sharpe=coin_edge.get("months_positive", coin_edge.get("sharpe")) if coin_edge else None,
            validated_return_pct=coin_edge.get("return_pct") if coin_edge else None,
        )

        opportunities.append(opp)
        time.sleep(0.3)

    # Sort by combined score descending
    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities


# ──────────────────────────────────────────────────────────────
#  State builder — shared between bias and entry phases
# ──────────────────────────────────────────────────────────────
def _build_state(
    candles: list[dict],
    mid_price: float,
    funding_rate: float,
    predicted_funding: float,
    premium: float,
    open_interest: float,
    oracle_price: float,
    day_volume: float,
    funding_divergence: float,
    breadth_ob: float,
    breadth_os: float,
    market_avg_rsi: float,
    btc_momentum: float,
    soak_data: dict | None = None,
) -> dict:
    """Build a signal-factory state dict from candle data + market context.

    If soak_data is provided (from MultiCoinFeed), real websocket-derived
    values replace approximations for CVD, volume_delta, relative_volume,
    tape, orderflow, and vault_positions.
    """
    closes = [float(c["c"]) for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    volumes = [float(c["v"]) for c in candles]

    rsi = _compute_rsi(closes, 14)
    ema_fast = _ema(closes, 20)
    ema_slow = _ema(closes, 50)
    atr = _compute_atr(highs, lows, closes, 14)
    bb_upper, bb_lower, bb_mid = _bollinger(closes, 20, 2.0)
    kc_upper, kc_lower = _keltner(closes, highs, lows, 20, 1.5)
    avg_vol = sum(volumes[-20:]) / min(20, len(volumes[-20:])) if volumes[-20:] else 1.0
    rel_vol = float(volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

    # Default: approximate CVD from candle direction
    recent_cvd = 0.0
    recent_vol_delta = 0.0
    for c in candles[-10:]:
        o, cl, v = float(c["o"]), float(c["c"]), float(c["v"])
        delta = v if cl > o else -v if cl < o else 0
        recent_cvd += delta
        recent_vol_delta = delta

    atr_val = float(atr) if atr > 0 else mid_price * 0.01

    state = {
        "price": mid_price,
        "close": mid_price,
        "price_history": closes,
        "volume_history": volumes,
        "indicators": {
            "rsi": rsi, "rsi_14": rsi,
            "ema_fast": ema_fast, "ema_slow": ema_slow,
            "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_mid": bb_mid,
            "kc_upper": kc_upper, "kc_lower": kc_lower,
            "atr": atr_val,
        },
        "funding_rate": funding_rate,
        "predicted_funding": predicted_funding,
        "premium": premium,
        "open_interest": open_interest,
        "oracle_price": oracle_price,
        "day_volume": day_volume,
        "oi_roc": 0,
        "oi_delta": 0,
        "oi_zscore": 0,
        "relative_volume": rel_vol,
        "cvd": recent_cvd,
        "volume_delta": recent_vol_delta,
        "funding_divergence": funding_divergence,
        "market_breadth_ob": breadth_ob,
        "market_breadth_os": breadth_os,
        "market_avg_rsi": market_avg_rsi,
        "btc_momentum": btc_momentum,
        "net_delta": 0,
        "bids_depth": 0,
        "asks_depth": 0,
    }

    # Override with real websocket data if soak was performed
    if soak_data:
        if soak_data.get("cvd"):
            state["cvd"] = soak_data["cvd"]
        if soak_data.get("volume_delta"):
            state["volume_delta"] = soak_data["volume_delta"]
        if soak_data.get("relative_volume", 1.0) != 1.0:
            state["relative_volume"] = soak_data["relative_volume"]

        # Real trade tape (enables trade_tape_imbalance signal)
        if soak_data.get("tape"):
            state["tape"] = soak_data["tape"]

        # Order flow from BBO (enables orderbook_imbalance signal)
        orderflow = soak_data.get("orderflow", {})
        if orderflow:
            state["orderflow"] = orderflow
            state["bids_depth"] = orderflow.get("avg_bid_depth", 0)
            state["asks_depth"] = orderflow.get("avg_ask_depth", 0)

        # Vault positions (enables vault_smart_money signal)
        if soak_data.get("vault_positions"):
            state["vault_positions"] = soak_data["vault_positions"]

    return state


# ──────────────────────────────────────────────────────────────
#  Indicator helpers (lightweight, used only for building state)
# ──────────────────────────────────────────────────────────────
def _ema(data: list[float], period: int) -> float:
    if len(data) < period:
        return data[-1] if data else 0.0
    alpha = 2 / (period + 1)
    ema = data[0]
    for val in data[1:]:
        ema = alpha * val + (1 - alpha) * ema
    return ema


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _compute_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return highs[-1] - lows[-1] if highs and lows else 0.0
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return sum(tr[-period:]) / period if tr else 0.0


def _bollinger(closes: list[float], period: int = 20, std_mult: float = 2.0):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    window = closes[-period:]
    sma = sum(window) / period
    std = (sum((x - sma) ** 2 for x in window) / period) ** 0.5
    return sma + std_mult * std, sma - std_mult * std, sma


def _keltner(closes: list[float], highs: list[float], lows: list[float],
             period: int = 20, mult: float = 1.5):
    ema_val = _ema(closes, period)
    atr_val = _compute_atr(highs, lows, closes, period)
    return ema_val + mult * atr_val, ema_val - mult * atr_val
