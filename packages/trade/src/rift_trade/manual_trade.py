"""Manual trade engine — one-click trade with live monitoring.

Places a single trade with stop loss on Hyperliquid and monitors it
until the user closes manually. No strategy running — the user IS
the strategy. Reuses live.py helpers for execution and monitoring.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from rift_data.data import get_info_client, fetch_predicted_funding, fetch_market_context
from rift_trade.builder_fee import get_builder_info, BUILDER_FEE_DISPLAY
from rift.algo import (
    AlgoPosition, AlgoTrade,
    _emit, _sanitize_for_json,
    _load_sz_decimals, _round_size, _round_price,
    _open_position_algo, _close_position_algo,
    _sync_position, _get_equity, _get_current_funding,
    _minutes_to_next_funding, _trade_replay_dict,
    ALGO_DIR,
)


def run_manual_trade(
    coin: str,
    side: str,
    size_usd: float,
    stop_pct: float = 0.02,
    leverage: int = 1,
    private_key: str = "",
    account_address: str = "",
) -> None:
    """Place a manual trade and monitor until user closes."""
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account

    from rift_data.data import normalize_coin
    coin = normalize_coin(coin)
    base_url = constants.MAINNET_API_URL
    builder_info = get_builder_info()

    if not private_key:
        _emit({"type": "error", "msg": "No API wallet key provided. Run: rift auth setup"})
        sys.exit(1)

    wallet = Account.from_key(private_key)
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    # Load precision
    sz_map = _load_sz_decimals(info)
    sz_decimals = sz_map.get(coin, 2)

    # Set leverage
    try:
        exchange.update_leverage(leverage, coin, is_cross=True)
    except Exception:
        pass

    # Query account collateral. Mode-aware so unified-account users
    # don't see "$0 perp" when their spot USDC is the actual margin pool.
    try:
        from rift_data.account_mode import read_collateral
        collateral = read_collateral(info, account_address)
        account_value = float(collateral.total)
    except Exception as e:
        _emit({"type": "error",
               "msg": f"Cannot query Hyperliquid account state: {e}. "
                      f"Check `rift doctor` for HL API connectivity and wallet config."})
        sys.exit(1)

    if account_value < 10:
        _emit({"type": "error",
               "msg": f"Insufficient balance: ${account_value:.2f} ({collateral.mode} mode) — "
                      f"Hyperliquid minimum order value is $10. "
                      f"Deposit USDC: `rift deposit` or via app.hyperliquid.xyz."})
        sys.exit(1)

    # Get current price
    mids = info.all_mids()
    price = float(mids.get(coin, "0"))
    if price <= 0:
        _emit({"type": "error",
               "msg": f"Cannot get price for {coin}: not in Hyperliquid's all_mids feed. "
                      f"Verify the coin symbol is supported on Hyperliquid (run `rift list-pairs`)."})
        sys.exit(1)

    # Compute position
    size = size_usd / price
    is_buy = side.lower() == "long"

    if is_buy:
        stop_price = price * (1 - stop_pct)
        sl_is_buy = False
    else:
        stop_price = price * (1 + stop_pct)
        sl_is_buy = True

    # Capture active signals at entry for signal memory. Skip the websocket
    # soak — manual trades shouldn't block 2 min on signal bookkeeping.
    entry_signal_names: list[str] = []
    try:
        from rift.scout import scan_market
        opps = scan_market(top_n=20, interval="1h", min_confluence=0, soak_seconds=0)
        for o in opps:
            if o.coin == coin and o.direction == side.upper():
                entry_signal_names = o.signal_names
                break
    except Exception:
        pass

    _emit({"type": "status", "msg": f"Placing {side.upper()} {coin} ${size_usd:.0f} @ ${price:,.2f} | Stop: ${stop_price:,.2f}"})

    # Place the order
    signal_ts = time.time()
    result = _open_position_algo(
        exchange, coin, is_buy, size, price,
        stop_price, sl_is_buy, stop_pct,
        builder_info, sz_decimals,
    )

    if not result:
        _emit({"type": "error",
               "msg": "Order failed: no fill returned from Hyperliquid. Common causes: "
                      "insufficient margin, price moved outside IOC limit, exchange rate-limit. "
                      "Check `rift more balance` and `rift doctor`; retry if transient."})
        sys.exit(1)

    fill_price = result.get("fill_price", price)
    filled_size = result.get("filled_size", size)
    stop_oid = result.get("stop_oid")
    entry_oid = result.get("entry_oid")

    position = AlgoPosition(
        side=side.lower(),
        entry_price=fill_price,
        size=filled_size,
        entry_time=datetime.now().strftime("%H:%M"),
        stop_oid=stop_oid,
        stop_price=result.get("stop_price", stop_price),
        entry_mid_price=result.get("mid_price_at_order", price),
        execution_method="ioc",
        signal_ts=signal_ts,
        submit_ts=result.get("submit_ts", 0),
        fill_ts=result.get("fill_ts", 0),
    )

    _emit({
        "type": "trade", "action": "open",
        "side": side.lower(),
        "price": round(fill_price, 2),
        "size": round(filled_size, 6),
        "stop_loss": round(stop_price, 2),
        "oid": entry_oid,
    })

    # ─── MONITORING LOOP ───
    initial_equity = account_value
    equity = initial_equity
    peak_equity = initial_equity
    total_funding = 0.0
    last_funding_hour = int(time.time() * 1000) // 3600000 * 3600000
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    price_history: list[float] = [price]
    session_start_price = price
    # Initialize funding_rate so the first 4 ticks (before the tick % 5 == 0
    # refresh) don't hit UnboundLocalError when computing predicted_funding.
    funding_rate = 0.0

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

            # Update price
            try:
                mids = info.all_mids()
                live_price = float(mids.get(coin, "0"))
                if live_price > 0:
                    price = live_price
                    price_history.append(price)
                    if len(price_history) > 30:
                        price_history = price_history[-30:]
            except Exception:
                pass

            # Update position excursion
            if position.side == "long":
                unreal = position.size * (price - position.entry_price)
            else:
                unreal = position.size * (position.entry_price - price)
            if unreal > position.max_favorable:
                position.max_favorable = unreal
            if unreal < position.max_adverse:
                position.max_adverse = unreal

            # Sync with exchange every 5 ticks
            if tick % 5 == 0:
                real_equity, real_pos = _sync_position(info, account_address, coin)
                if real_equity > 0:
                    equity = real_equity
                if equity > peak_equity:
                    peak_equity = equity

                # Check if stopped out
                if real_pos is None:
                    _emit({"type": "trade", "action": "stop_loss"})
                    _emit({"type": "status", "msg": "Stop loss triggered by Hyperliquid"})
                    running = False
                    break

                # Funding tracking
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

            # Stop proximity
            stop_prox = 0.0
            if position.stop_price > 0:
                if position.side == "long":
                    denom = position.entry_price - position.stop_price
                    if denom > 0:
                        stop_prox = max(0, min(1, (position.entry_price - price) / denom))
                else:
                    denom = position.stop_price - position.entry_price
                    if denom > 0:
                        stop_prox = max(0, min(1, (price - position.entry_price) / denom))

            # Funding countdown
            mins_to_funding = _minutes_to_next_funding()
            pv = position.size * price
            predicted_funding = (-pv * funding_rate) if position.side == "long" else (pv * funding_rate) if tick % 5 == 0 else 0

            total_pnl_pct = (equity - initial_equity) / initial_equity * 100 if initial_equity > 0 else 0

            _emit({
                "type": "heartbeat",
                "state": _sanitize_for_json({
                    "strategy": "manual", "pair": f"{coin}-PERP", "interval": "manual",
                    "equity": round(equity, 2), "total_equity": round(equity, 2),
                    "unrealized_pnl": round(unreal, 2),
                    "total_pnl": round(equity - initial_equity, 2),
                    "total_pnl_pct": round(total_pnl_pct, 2),
                    "initial_equity": round(initial_equity, 2),
                    "position": {
                        "side": position.side, "entry_price": round(position.entry_price, 2),
                        "size": round(position.size, 6), "candles_held": 0,
                        "stop_loss_price": round(position.stop_price, 2),
                        "funding_collected": round(position.funding_collected, 2),
                    },
                    "last_price": round(price, 2),
                    "last_funding_rate": funding_rate if tick % 5 == 0 else 0,
                    "total_funding": round(total_funding, 2),
                    "num_trades": 0, "win_rate": 0,
                    "price_history": [round(p, 2) for p in price_history[-30:]],
                    "price_delta": round(price - session_start_price, 2),
                    "session_high": round(max(price_history), 2),
                    "session_low": round(min(price_history), 2),
                    "stop_proximity": round(stop_prox, 3),
                    "peak_equity": round(peak_equity, 2),
                    "is_live": True, "wallet": account_address,
                }),
                "funding_countdown_min": mins_to_funding,
                "predicted_funding": round(predicted_funding, 4),
            })

            time.sleep(1)

        except Exception as e:
            _emit({"type": "error",
                   "msg": f"Manual-trade monitor loop hit a transient error: {type(e).__name__}: {e}. "
                          f"Continuing — if this repeats, the position may be unmonitored; "
                          f"Ctrl+C to close the position and investigate."})
            time.sleep(1)

    # ─── CLOSE POSITION ───
    _emit({"type": "status", "msg": "Closing position..."})
    trade = _close_position_algo(exchange, info, position, coin, account_address, builder_info, sz_decimals)
    equity = _get_equity(info, account_address)

    # Session summary
    total_pnl = equity - initial_equity

    if trade:
        _emit({"type": "trade", "action": "close", "trade_replay": _trade_replay_dict(trade)})

        # Log to signal memory for learning
        if entry_signal_names:
            try:
                from rift.signal_memory import record_outcome
                pnl_pct = (total_pnl / initial_equity * 100) if initial_equity > 0 else 0
                record_outcome(coin=coin, direction=side.lower(), signals=entry_signal_names, pnl_pct=pnl_pct, source="manual")
            except Exception:
                pass

    # Shareable card (plain text, no ANSI)
    card_lines = [
        "╔═══════════════════════════════════════╗",
        "║  RIFT TRADE                           ║",
        "╠═══════════════════════════════════════╣",
        f"║  {side.upper()} {coin}".ljust(40) + "║",
        "║                                       ║",
        f"║  Entry:    ${position.entry_price:,.2f}".ljust(40) + "║",
        f"║  Exit:     ${trade.exit_price:,.2f}".ljust(40) + "║" if trade else "║  Exit:     (stopped)".ljust(40) + "║",
        f"║  P&L:      ${total_pnl:+,.2f} ({total_pnl/initial_equity*100:+.1f}%)".ljust(40) + "║" if initial_equity > 0 else "║".ljust(40) + "║",
        f"║  Funding:  ${total_funding:+,.2f}".ljust(40) + "║",
        "║                                       ║",
        "║  nexstone.io/rift                     ║",
        "╚═══════════════════════════════════════╝",
    ]

    _emit({
        "type": "shutdown",
        "msg": "Trade closed",
        "state": _sanitize_for_json({
            "strategy": "manual", "pair": f"{coin}-PERP",
            "equity": round(equity, 2), "total_equity": round(equity, 2),
            "initial_equity": round(initial_equity, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / initial_equity * 100, 2) if initial_equity > 0 else 0,
            "total_funding": round(total_funding, 2),
            "num_trades": 1,
            "wallet": account_address,
        }),
        "trade_replay": _trade_replay_dict(trade) if trade else None,
        "shareable_card": "\n".join(card_lines),
    })
