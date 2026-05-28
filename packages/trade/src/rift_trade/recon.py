"""Recon — execute a Scout opportunity with tape confirmation.

The soldier. Scout is the intelligence officer that delivers a complete
mission brief (coin, direction, leverage, size, stop, target, hold type).
Recon confirms with live websocket data, executes, manages, and reports.

Places real orders on Hyperliquid (requires auth + builder fee).

Flow:
    1. Receive Opportunity from Scout
    2. Start LiveMarketFeed for the target coin
    3. Wait for tape confirmation (trade flow agrees with direction)
    4. Execute entry + stop loss on exchange
    5. Monitor position with hold limits from mission brief
    6. Close on target/stop/max hold/user exit
    7. Record outcome to signal memory + save trade log
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from rift_data.data import get_info_client, fetch_predicted_funding
from rift_trade.builder_fee import get_builder_info, BUILDER_FEE_DISPLAY
from rift_data.ws_feed import LiveMarketFeed
from rift_engine.signals.aggregator import Opportunity
from rift_substrate.execution import PositionState, resolve_policy
from rift.algo import (
    AlgoPosition, AlgoTrade,
    _emit, _sanitize_for_json,
    _load_sz_decimals, _round_size, _round_price,
    _open_position_algo, _close_position_algo,
    _sync_position, _get_equity, _get_current_funding,
    _minutes_to_next_funding, _trade_replay_dict,
)

# Max hold times per hold_type label live in substrate exit policies — see
# rift_substrate.execution.exit_policies and resolve_policy(). recon picks the
# policy at entry via opportunity.hold_type; the policy declares its own
# max_hold_seconds and owns dynamic stop management.

# Sizing constraint — cap position at 1% of 24h volume to keep slippage bounded.
RECON_VOLUME_CAP = 0.01

# Execution parameters
PULLBACK_WAIT_SECS = 120   # max seconds to wait for a pullback entry
PULLBACK_PCT = 0.002       # 0.2% retracement qualifies as a pullback
LIMIT_WAIT_SECS = 30       # seconds to wait for limit order fill before escalating
LIMIT_ESCALATE_SECS = 15   # seconds between escalation attempts


def run_recon(
    opportunity: Opportunity,
    private_key: str = "",
    account_address: str = "",
    confirm_seconds: int = 120,
    no_guard: bool = False,
    size_usd_override: float = 0.0,
) -> None:
    """Execute a Scout opportunity with tape confirmation.

    Args:
        opportunity: Complete mission brief from Scout
        private_key: Hyperliquid API wallet key
        account_address: Main wallet address
        confirm_seconds: Max seconds to wait for tape confirmation
        size_usd_override: When > 0, bypass scout's kelly/confluence sizing
            and use this USD value instead. Volume cap and correlation guard
            still apply. Intended for small-account testing — every user
            verifying their setup with a tiny first trade hits the same
            "size below $10 HL minimum" wall otherwise. The override is
            announced loudly in the output so it can't be used silently.
    """
    coin = opportunity.coin
    direction = opportunity.direction.lower()
    is_buy = direction == "long"
    mode_label = "RECON"

    # ══════════════════════════════════════════════════════
    #  PHASE A: SETUP — real exchange, real money
    # ══════════════════════════════════════════════════════
    info = get_info_client()

    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants
    from eth_account import Account

    builder_info = get_builder_info()
    from rift_trade.builder_fee import BUILDER_ADDRESS
    if len(BUILDER_ADDRESS) != 42 or BUILDER_ADDRESS[:5] != "0x091":
        _emit({"type": "error",
               "msg": "Builder fee address invalid — this RIFT install is tampered or corrupted. "
                      "Reinstall from source: rm -rf engine/.venv packages/cli/node_modules && "
                      "re-run `uv sync` and `pnpm install` from a fresh clone."})
        return
    base_url = constants.MAINNET_API_URL

    if not private_key:
        _emit({"type": "error", "msg": "No API wallet key provided. Run: rift auth setup"})
        sys.exit(1)

    wallet = Account.from_key(private_key)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    sz_map = _load_sz_decimals(info)
    sz_decimals = sz_map.get(coin, 2)

    try:
        exchange.update_leverage(opportunity.leverage, coin, is_cross=True)
    except Exception:
        pass

    try:
        from rift_data.account_mode import read_collateral
        equity = float(read_collateral(info, account_address).total)
    except Exception as e:
        _emit({"type": "error",
               "msg": f"Cannot query Hyperliquid account state: {e}. "
                      f"Check `rift doctor` for HL API connectivity and wallet config."})
        sys.exit(1)

    if equity < 10:
        _emit({"type": "error",
               "msg": f"Insufficient balance: ${equity:.2f} (Hyperliquid minimum order value is $10). "
                      f"Deposit USDC: `rift deposit` or via app.hyperliquid.xyz."})
        sys.exit(1)

    # Compute position size — either from scout's mission brief, or from
    # the explicit override (for small-account testing of the recon path).
    if size_usd_override > 0:
        size_usd = size_usd_override
        _emit({
            "type": "status",
            "msg": f"⚠ SIZE OVERRIDE: using ${size_usd_override:.2f} (bypasses scout's "
                   f"kelly/confluence risk model; volume cap still applies). "
                   f"Computed model size would have been "
                   f"${equity * opportunity.size_pct * opportunity.leverage:.2f}.",
        })
    else:
        size_usd = equity * opportunity.size_pct * opportunity.leverage
    price = float(info.all_mids().get(coin, "0"))
    if price <= 0:
        _emit({"type": "error",
               "msg": f"Cannot get price for {coin}: not in Hyperliquid's all_mids feed. "
                      f"Verify the coin symbol is supported on Hyperliquid (run `rift list-pairs`)."})
        sys.exit(1)

    # Volume cap: limit position to 1% of 24h volume
    if opportunity.volume_24h > 0:
        volume_cap_usd = opportunity.volume_24h * RECON_VOLUME_CAP
        if size_usd > volume_cap_usd:
            size_usd = volume_cap_usd

    size = size_usd / price

    stop_price = opportunity.stop_price
    target_price = opportunity.target_price
    stop_pct = abs(price - stop_price) / price if price > 0 else 0.02

    _emit({
        "type": "status",
        "msg": f"{mode_label} — {direction.upper()} {coin}",
        "brief": {
            "mode": "live",
            "direction": direction.upper(),
            "coin": coin,
            "score": opportunity.score,
            "confidence": opportunity.confidence_tier,
            "leverage": f"{opportunity.leverage}x",
            "size_usd": round(size_usd, 2),
            "size_pct": f"{opportunity.size_pct * 100:.1f}%",
            "equity": round(equity, 2),
            "entry": round(price, 6),
            "stop": round(stop_price, 6),
            "target": round(target_price, 6),
            "hold_type": opportunity.hold_type,
            "staleness": f"{opportunity.staleness_minutes}min",
            "categories": opportunity.num_categories,
            "signals": opportunity.num_signals,
        },
    })

    # ══════════════════════════════════════════════════════
    #  CORRELATION GUARD — prevent sector doubling
    # ══════════════════════════════════════════════════════
    from rift.correlation_guard import check_correlation

    corr = check_correlation(coin, direction) if not no_guard else {"blocked": False, "warning": False, "reduce_size": False, "msg": ""}
    if corr["blocked"]:
        _emit({"type": "abort", "reason": "correlation_guard", "msg": corr["msg"]})
        return
    if corr["reduce_size"]:
        size_usd *= 0.5
        _emit({"type": "status", "msg": f"Correlation guard: {corr['msg']}"})
    elif corr["warning"]:
        _emit({"type": "status", "msg": f"Correlation warning: {corr['msg']}"})

    # ══════════════════════════════════════════════════════
    #  PHASE B: TAPE CONFIRMATION
    # ══════════════════════════════════════════════════════
    _emit({"type": "status", "msg": f"Starting tape confirmation ({confirm_seconds}s window)..."})

    feed = LiveMarketFeed(coin)
    feed.start()

    confirmed = False
    confirm_start = time.time()

    while time.time() - confirm_start < confirm_seconds:
        elapsed = int(time.time() - confirm_start)

        ws_data = feed.get_derived()
        tape = ws_data.get("tape", {})
        imbalance = tape.get("imbalance", 0)
        trade_count = tape.get("trade_count", 0)

        if trade_count >= 10:
            if is_buy and imbalance > 0.1:
                confirmed = True
                _emit({"type": "status", "msg": f"Tape confirmed LONG — imbalance {imbalance:+.3f} ({trade_count} trades)"})
                break
            elif not is_buy and imbalance < -0.1:
                confirmed = True
                _emit({"type": "status", "msg": f"Tape confirmed SHORT — imbalance {imbalance:+.3f} ({trade_count} trades)"})
                break

        if elapsed % 5 == 0:
            orderflow = ws_data.get("orderflow", {})
            _emit({
                "type": "soak",
                "phase": "confirmation",
                "elapsed": elapsed,
                "total": confirm_seconds,
                "coin": coin,
                "direction": direction,
                "imbalance": round(imbalance, 3),
                "trades": trade_count,
                "cvd": round(ws_data.get("cvd", 0), 4),
                "buy_volume": tape.get("buy_volume", 0),
                "sell_volume": tape.get("sell_volume", 0),
                "tape_speed": tape.get("tape_speed", 0),
                "vwap": tape.get("vwap", 0),
                "bid_ratio": orderflow.get("bid_ratio", 0.5),
                "relative_volume": ws_data.get("relative_volume", 1.0),
            })

        time.sleep(5)

    if not confirmed:
        _emit({"type": "abort", "reason": "tape_not_confirmed",
               "msg": f"Tape did not confirm {direction.upper()} within {confirm_seconds}s. Aborting."})
        feed.stop()
        return

    # ══════════════════════════════════════════════════════
    #  PHASE C1: PULLBACK ENTRY — wait for a better price
    # ══════════════════════════════════════════════════════
    price = float(info.all_mids().get(coin, "0"))
    confirm_price = price  # price at tape confirmation

    _emit({"type": "status", "msg": f"Waiting for pullback entry ({PULLBACK_PCT*100:.1f}% retracement, max {PULLBACK_WAIT_SECS}s)..."})

    pullback_start = time.time()
    best_entry = price
    got_pullback = False

    while time.time() - pullback_start < PULLBACK_WAIT_SECS:
        try:
            mids = info.all_mids()
            live_px = float(mids.get(coin, "0"))
            if live_px <= 0:
                time.sleep(1)
                continue
            price = live_px
        except Exception:
            time.sleep(1)
            continue

        # For a LONG: we want price to dip below confirm_price
        # For a SHORT: we want price to bounce above confirm_price
        if is_buy:
            retrace = (confirm_price - price) / confirm_price
            if retrace >= PULLBACK_PCT:
                got_pullback = True
                best_entry = price
                _emit({"type": "status", "msg": f"Pullback detected: ${price:,.6g} ({retrace*100:.2f}% below confirmation)"})
                break
        else:
            retrace = (price - confirm_price) / confirm_price
            if retrace >= PULLBACK_PCT:
                got_pullback = True
                best_entry = price
                _emit({"type": "status", "msg": f"Pullback detected: ${price:,.6g} ({retrace*100:.2f}% above confirmation)"})
                break

        pullback_elapsed = int(time.time() - pullback_start)
        if pullback_elapsed % 5 == 0:
            retrace_val = retrace if 'retrace' in dir() else 0
            _emit({
                "type": "soak",
                "phase": "pullback",
                "elapsed": pullback_elapsed,
                "total": PULLBACK_WAIT_SECS,
                "coin": coin,
                "direction": direction,
                "current_price": round(price, 6),
                "confirm_price": round(confirm_price, 6),
                "retrace_pct": round(retrace_val * 100, 3),
                "target_retrace_pct": round(PULLBACK_PCT * 100, 3),
            })

        time.sleep(2)

    if not got_pullback:
        best_entry = price
        _emit({"type": "status", "msg": f"No pullback within {PULLBACK_WAIT_SECS}s — entering at market ${price:,.6g}"})

    # ══════════════════════════════════════════════════════
    #  PHASE C2: EXECUTE — limit-first, escalate to IOC
    # ══════════════════════════════════════════════════════
    price = best_entry
    size = size_usd / price

    # Recalculate stop from ATR distance at current price
    atr_dist = abs(opportunity.entry_price - opportunity.stop_price)
    if is_buy:
        stop_price = price - atr_dist
    else:
        stop_price = price + atr_dist
    stop_pct = abs(price - stop_price) / price

    signal_ts = time.time()

    # Limit-first, escalate to IOC if no fill
    sl_is_buy = not is_buy
    builder_info = get_builder_info()

    # Attempt 1: Post limit order at current mid
    _emit({"type": "status", "msg": f"Posting limit {direction.upper()} {coin} @ ${price:,.6g}..."})

    result = None
    limit_px = _round_price(price, sz_decimals)
    rounded_size = _round_size(size, sz_decimals)

    try:
        limit_result = exchange.order(
            coin, is_buy, rounded_size, limit_px,
            order_type={"limit": {"tif": "Gtc"}},  # Good til cancelled
            reduce_only=False,
            builder=builder_info,
        )

        # Wait for fill
        fill_deadline = time.time() + LIMIT_WAIT_SECS
        while time.time() < fill_deadline:
            time.sleep(3)
            real_eq, real_pos = _sync_position(info, account_address, coin)
            if real_pos is not None:
                # Filled — extract details
                filled_size = abs(float(real_pos.get("szi", "0")))
                fill_price = float(real_pos.get("entryPx", str(price)))
                if filled_size > 0:
                    _emit({"type": "status", "msg": f"Limit filled: {filled_size} @ ${fill_price:,.6g}"})
                    result = {"fill_price": fill_price, "filled_size": filled_size}
                    break

        # Cancel unfilled limit if still resting
        if result is None:
            try:
                open_orders = info.open_orders(account_address)
                for o in open_orders:
                    if o.get("coin") == coin:
                        exchange.cancel(coin, o["oid"])
            except Exception:
                pass
    except Exception as e:
        _emit({"type": "status", "msg": f"Limit order failed: {e}"})

    # Attempt 2: Escalate to IOC if limit didn't fill
    if result is None:
        _emit({"type": "status", "msg": f"Limit not filled — escalating to IOC market order"})
        price = float(info.all_mids().get(coin, "0"))
        size = size_usd / price

        result = _open_position_algo(
            exchange, coin, is_buy, size, price,
            stop_price, sl_is_buy, stop_pct,
            builder_info, sz_decimals,
        )

        if not result:
            _emit({"type": "error", "msg": "Order failed — no fill on limit or IOC"})
            feed.stop()
            return

        fill_price = result.get("fill_price", price)
        filled_size = result.get("filled_size", size)
        exec_method = "ioc_escalated"
    else:
        fill_price = result["fill_price"]
        filled_size = result["filled_size"]
        exec_method = "limit_filled"

        # Place stop loss separately (limit fill doesn't use normalTpsl).
        # If stop placement throws or HL rejects, the just-filled position
        # is sitting on the exchange without protection — close it
        # immediately. Continuing without an exchange-side stop AND no
        # local-monitor stop fallback would leave the position unprotected
        # up to max_hold_seconds (30 min for mean_reversion, 4-8h for
        # others), which is unacceptable.
        stop_placement_failed = False
        try:
            sl_resp = exchange.order(
                coin, sl_is_buy, filled_size,
                _round_price(stop_price, sz_decimals),
                order_type={"trigger": {"triggerPx": _round_price(stop_price, sz_decimals), "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
                builder=builder_info,
            )
            if isinstance(sl_resp, dict) and sl_resp.get("status") != "ok":
                stop_placement_failed = True
                _emit({"type": "error", "msg": f"Stop loss rejected by exchange: {sl_resp}"})
        except Exception as e:
            stop_placement_failed = True
            _emit({"type": "error", "msg": f"Stop loss placement FAILED: {e}"})

        if stop_placement_failed:
            _emit({"type": "status", "msg": "Closing position immediately — refuse to ride without stop protection."})
            try:
                exchange.order(
                    coin, not is_buy, filled_size,
                    _round_price(fill_price, sz_decimals),
                    order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=True,
                    builder=builder_info,
                )
                _emit({"type": "status", "msg": "Emergency close: position flat."})
            except Exception as close_err:
                _emit({"type": "error",
                       "msg": f"CRITICAL: emergency close failed: {close_err}. "
                              f"Close manually via Hyperliquid UI immediately."})
            feed.stop()
            return

    position = AlgoPosition(
        side=direction,
        entry_price=fill_price,
        size=filled_size,
        entry_time=datetime.now().strftime("%H:%M"),
        stop_price=stop_price,
        entry_mid_price=price,
        execution_method=exec_method,
        signal_ts=signal_ts,
        submit_ts=result.get("submit_ts", 0) if isinstance(result, dict) else 0,
        fill_ts=result.get("fill_ts", 0) if isinstance(result, dict) else 0,
    )

    _emit({
        "type": "trade", "action": "open",
        "mode": "live",
        "side": direction, "coin": coin,
        "price": round(fill_price, 6),
        "size": round(filled_size, 6),
        "stop_loss": round(stop_price, 6),
        "target": round(target_price, 6),
        "leverage": opportunity.leverage,
        "hold_type": opportunity.hold_type,
        "execution": position.execution_method,
        "pullback": got_pullback,
    })

    # ══════════════════════════════════════════════════════
    #  PHASE D: MONITOR (with dynamic stop management)
    # ══════════════════════════════════════════════════════
    initial_equity = equity
    peak_equity = equity
    total_funding = 0.0
    last_funding_hour = int(time.time() * 1000) // 3600000 * 3600000
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry_time = time.time()
    price_history: list[float] = [price]
    funding_rate = 0.0
    exit_policy = resolve_policy(opportunity.hold_type)
    max_hold_seconds = exit_policy.max_hold_seconds
    exit_reason = "user"

    # Dynamic stop tracking
    original_stop = stop_price
    peak_price = fill_price  # best price in our favor
    stop_moved_to_breakeven = False
    last_stop_update = 0.0
    max_favorable_time = entry_time  # when peak excursion was reached

    running = True

    def handle_shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    tick = 0
    while running:
        try:
            tick += 1
            elapsed = time.time() - entry_time

            # Update price
            try:
                mids = info.all_mids()
                live_price = float(mids.get(coin, "0"))
                if live_price > 0:
                    price = live_price
                    price_history.append(price)
                    if len(price_history) > 60:
                        price_history = price_history[-60:]
            except Exception:
                pass

            # Excursion tracking
            if position.side == "long":
                unreal = position.size * (price - position.entry_price)
            else:
                unreal = position.size * (position.entry_price - price)
            if unreal > position.max_favorable:
                position.max_favorable = unreal
                max_favorable_time = time.time()
            if unreal < position.max_adverse:
                position.max_adverse = unreal

            # ─── DYNAMIC STOP MANAGEMENT ───

            # Track peak price in our favor
            if position.side == "long" and price > peak_price:
                peak_price = price
            elif position.side == "short" and price < peak_price:
                peak_price = price

            # Delegate dynamic stop management to the substrate exit policy.
            # The policy was resolved at entry from opportunity.hold_type; it
            # owns all per-style behaviour (trailing, widen-on-funding, tighten-
            # on-elapsed). recon just builds a state snapshot and applies the
            # returned action.
            state = PositionState(
                side=position.side,
                entry_price=fill_price,
                current_price=price,
                peak_price=peak_price,
                elapsed_seconds=elapsed,
                current_stop=stop_price,
                original_stop=original_stop,
                atr_dist=atr_dist,
                funding_collected=total_funding,
                breakeven_reached=stop_moved_to_breakeven,
            )
            action = exit_policy.update(state)
            if action.new_stop is not None:
                stop_price = action.new_stop
            if action.breakeven_reached:
                stop_moved_to_breakeven = True
            if action.status_message:
                _emit({"type": "status", "msg": action.status_message})
            if action.close_position:
                exit_reason = "policy"
                running = False
                break

            # Stall detection: if price hasn't moved 0.1% in staleness*2, tighten
            if elapsed > opportunity.staleness_minutes * 120:  # staleness * 2 in seconds
                if len(price_history) >= 10:
                    recent_range = max(price_history[-10:]) - min(price_history[-10:])
                    if recent_range / price < 0.001:  # less than 0.1% range
                        if position.side == "long":
                            stall_stop = price - atr_dist * 0.3
                            if stall_stop > stop_price:
                                stop_price = stall_stop
                        else:
                            stall_stop = price + atr_dist * 0.3
                            if stall_stop < stop_price:
                                stop_price = stall_stop
                        _emit({"type": "status", "msg": f"Trade stalling — stop tightened to ${stop_price:,.6g}"})
                        exit_reason = "stall"
                        running = False
                        break

            # ─── EXIT CONDITIONS ───

            # Target hit
            if position.side == "long" and price >= target_price:
                _emit({"type": "status", "msg": f"Target hit at ${price:,.6g}"})
                exit_reason = "target"
                running = False
                break
            elif position.side == "short" and price <= target_price:
                _emit({"type": "status", "msg": f"Target hit at ${price:,.6g}"})
                exit_reason = "target"
                running = False
                break

            # Local stop check — defense-in-depth. The exchange-side stop
            # is the primary protection; this fires only if HL hasn't
            # closed us (we see the position is still real) but the mid
            # has crossed our stop. Catches the rare case where the
            # exchange stop disappeared / was cancelled, and the more
            # common case where HL's stop has a slightly different mid
            # reference than the public all_mids feed.
            if position.side == "long" and price <= stop_price:
                _emit({"type": "status", "msg": f"Local stop hit at ${price:,.6g} (exchange may close concurrently)"})
                exit_reason = "stop_loss"
                running = False
                break
            elif position.side == "short" and price >= stop_price:
                _emit({"type": "status", "msg": f"Local stop hit at ${price:,.6g} (exchange may close concurrently)"})
                exit_reason = "stop_loss"
                running = False
                break

            # Max hold exceeded
            if elapsed > max_hold_seconds:
                _emit({"type": "status", "msg": f"Max hold ({hold_type}: {max_hold_seconds // 60}min) exceeded"})
                exit_reason = "max_hold"
                running = False
                break

            # Sync with exchange every 5 ticks
            if tick % 5 == 0:
                real_equity, real_pos = _sync_position(info, account_address, coin)
                if real_equity > 0:
                    equity = real_equity
                if equity > peak_equity:
                    peak_equity = equity

                if real_pos is None:
                    _emit({"type": "trade", "action": "stop_loss"})
                    exit_reason = "stop_loss"
                    running = False
                    break

                funding_rate = _get_current_funding(info, coin)
                current_hour = int(time.time() * 1000) // 3600000 * 3600000
                if current_hour > last_funding_hour and funding_rate != 0:
                    pos_value = position.size * price
                    if position.side == "long":
                        funding_payment = -pos_value * funding_rate
                    else:
                        funding_payment = pos_value * funding_rate
                    total_funding += funding_payment
                    position.funding_collected += funding_payment
                    last_funding_hour = current_hour

            # Get live tape data for heartbeat
            ws_data = feed.get_derived()

            mins_to_funding = _minutes_to_next_funding()
            total_pnl_pct = (equity - initial_equity) / initial_equity * 100 if initial_equity > 0 else 0

            _emit({
                "type": "heartbeat",
                "state": _sanitize_for_json({
                    "strategy": "recon", "pair": f"{coin}-PERP",
                    "equity": round(equity, 2),
                    "unrealized_pnl": round(unreal, 2),
                    "total_pnl": round(equity - initial_equity, 2),
                    "total_pnl_pct": round(total_pnl_pct, 2),
                    "initial_equity": round(initial_equity, 2),
                    "position": {
                        "side": position.side,
                        "entry_price": round(position.entry_price, 6),
                        "size": round(position.size, 6),
                        "stop_loss_price": round(stop_price, 6),
                        "target_price": round(target_price, 6),
                        "funding_collected": round(position.funding_collected, 2),
                    },
                    "last_price": round(price, 6),
                    "total_funding": round(total_funding, 2),
                    "hold_type": opportunity.hold_type,
                    "hold_elapsed_min": round(elapsed / 60, 1),
                    "hold_max_min": max_hold_seconds // 60,
                    "confidence": opportunity.confidence_tier,
                    "leverage": opportunity.leverage,
                    "tape_imbalance": ws_data.get("tape", {}).get("imbalance", 0),
                    "cvd": round(ws_data.get("cvd", 0), 4),
                    "is_live": True,
                }),
                "funding_countdown_min": mins_to_funding,
            })

            time.sleep(1)

        except Exception as e:
            _emit({"type": "error",
                   "msg": f"Recon monitor loop hit a transient error: {type(e).__name__}: {e}. "
                          f"Continuing — if this repeats, the daemon is degraded; "
                          f"consider Ctrl+C and investigating ~/.rift/recon/ logs."})
            time.sleep(1)

    # ══════════════════════════════════════════════════════
    #  PHASE E: CLOSE + RECORD
    # ══════════════════════════════════════════════════════
    feed.stop()

    trade = None
    exit_px = price

    if exit_reason != "stop_loss":
        _emit({"type": "status", "msg": "Closing position..."})
        builder_info = get_builder_info()
        trade = _close_position_algo(exchange, info, position, coin, account_address, builder_info, sz_decimals)
    equity = _get_equity(info, account_address)
    total_pnl = equity - initial_equity
    exit_px = trade.exit_price if trade else price

    # Record outcome to signal memory
    pnl_pct = (total_pnl / initial_equity * 100) if initial_equity > 0 else 0
    try:
        from rift.signal_memory import record_outcome
        time_to_peak_min = (max_favorable_time - entry_time) / 60.0
        record_outcome(
            coin=coin,
            direction=direction,
            signals=opportunity.signal_names,
            pnl_pct=pnl_pct,
            source="recon",
            hold_minutes=elapsed_min,
            time_to_peak_minutes=round(time_to_peak_min, 1),
        )
        _emit({"type": "status", "msg": f"Outcome recorded to signal memory ({pnl_pct:+.2f}%)"})
    except Exception:
        pass

    # ─── POST-TRADE TCA ───
    entry_slippage_bps = 0.0
    if position.entry_mid_price > 0 and position.entry_price > 0:
        raw_slip = (position.entry_price - position.entry_mid_price) / position.entry_mid_price * 10000
        entry_slippage_bps = raw_slip if is_buy else -raw_slip

    pullback_savings_pct = 0.0
    if confirm_price > 0 and position.entry_price > 0:
        if is_buy:
            pullback_savings_pct = (confirm_price - position.entry_price) / confirm_price * 100
        else:
            pullback_savings_pct = (position.entry_price - confirm_price) / confirm_price * 100

    timing_alpha_bps = pullback_savings_pct * 100  # convert % to bps

    try:
        from rift_engine.tca import _compute_grade
        exec_score, exec_grade = _compute_grade(abs(entry_slippage_bps), atr_dist / fill_price * 10000 if fill_price > 0 else 100)
    except Exception:
        exec_score, exec_grade = 0, "N/A"

    _emit({"type": "status", "msg": f"Execution: grade {exec_grade} | slippage {entry_slippage_bps:+.1f}bps | pullback saved {pullback_savings_pct:+.3f}%"})

    # Save session log to disk
    elapsed_min = round((time.time() - entry_time) / 60, 1)
    session_log = {
        "type": "recon",
        "coin": coin,
        "direction": direction,
        "entry_price": round(position.entry_price, 6),
        "exit_price": round(exit_px, 6),
        "size": round(position.size, 6),
        "leverage": opportunity.leverage,
        "stop_price": round(position.stop_price, 6),
        "target_price": round(target_price, 6),
        "pnl_usd": round(total_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "funding_collected": round(total_funding, 2),
        "exit_reason": exit_reason,
        "hold_type": opportunity.hold_type,
        "hold_minutes": elapsed_min,
        "confidence_tier": opportunity.confidence_tier,
        "score": opportunity.score,
        "num_categories": opportunity.num_categories,
        "num_signals": opportunity.num_signals,
        "signal_names": opportunity.signal_names,
        "hit_rate": opportunity.hit_rate,
        "size_pct": opportunity.size_pct,
        "initial_equity": round(initial_equity, 2),
        "final_equity": round(equity, 2),
        "max_favorable": round(position.max_favorable, 2),
        "max_adverse": round(position.max_adverse, 2),
        "time_to_peak_minutes": round(time_to_peak_min, 1),
        "entry_slippage_bps": round(entry_slippage_bps, 2),
        "pullback_savings_pct": round(pullback_savings_pct, 3),
        "timing_alpha_bps": round(timing_alpha_bps, 2),
        "execution_grade": exec_grade,
        "execution_score": exec_score,
        "confirm_price": round(confirm_price, 6),
        "execution_method": getattr(position, 'execution_method', 'unknown'),
        "started_at": started_at,
        "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "account": account_address,
    }

    log_dir = Path.home() / ".rift" / "recon"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{coin}_{direction}.json"
    log_path = log_dir / log_name
    log_path.write_text(json.dumps(session_log, indent=2))
    _emit({"type": "status", "msg": f"Trade log saved: {log_path}"})

    # Shareable card
    card_label = "RIFT RECON"
    card_lines = [
        "╔═══════════════════════════════════════╗",
        f"║  {card_label}".ljust(40) + "║",
        "╠═══════════════════════════════════════╣",
        f"║  {direction.upper()} {coin} ({opportunity.confidence_tier.upper()})".ljust(40) + "║",
        f"║  {opportunity.leverage}x | {opportunity.hold_type}".ljust(40) + "║",
        "║                                       ║",
        f"║  Entry:    ${position.entry_price:,.6g}".ljust(40) + "║",
        f"║  Exit:     ${exit_px:,.6g} ({exit_reason})".ljust(40) + "║",
        f"║  P&L:      ${total_pnl:+,.2f} ({pnl_pct:+.1f}%)".ljust(40) + "║",
        f"║  Funding:  ${total_funding:+,.2f}".ljust(40) + "║",
        f"║  Score:    {opportunity.score:.3f} | Cats: {opportunity.num_categories}".ljust(40) + "║",
        "║                                       ║",
        "║  nexstone.io/rift                     ║",
        "╚═══════════════════════════════════════╝",
    ]

    _emit({
        "type": "shutdown",
        "msg": f"{mode_label} complete — {exit_reason}",
        "state": _sanitize_for_json({
            "strategy": "recon", "pair": f"{coin}-PERP",
            "equity": round(equity, 2),
            "initial_equity": round(initial_equity, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(pnl_pct, 2),
            "total_funding": round(total_funding, 2),
            "exit_reason": exit_reason,
            "hold_type": opportunity.hold_type,
            "confidence": opportunity.confidence_tier,
            "leverage": opportunity.leverage,
            "num_categories": opportunity.num_categories,
            "score": opportunity.score,
        }),
        "trade_replay": _trade_replay_dict(trade) if trade else None,
        "shareable_card": "\n".join(card_lines),
        "tca_summary": {
            "slippage_bps": round(entry_slippage_bps, 2),
            "pullback_savings_pct": round(pullback_savings_pct, 3),
            "timing_alpha_bps": round(timing_alpha_bps, 2),
            "execution_grade": exec_grade,
            "execution_method": getattr(position, 'execution_method', 'unknown'),
            "got_pullback": got_pullback,
        },
    })
