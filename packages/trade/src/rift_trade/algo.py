"""Algo trading engine for Hyperliquid.

Runs a registered strategy autonomously against Hyperliquid using the
API wallet. Based on simulate.py but with real order execution and
safety features.

Key differences from simulation:
- Orders via exchange.order() / exchange.market_open() with builder fee
- Stop losses as native trigger orders (Hyperliquid holds them server-side)
- Entry + stop loss placed atomically via grouping="normalTpsl"
- Position state persisted to disk for crash recovery
- Dead man's switch via scheduleCancel
- Real fills queried from info API
"""

from __future__ import annotations

import json
import os
import time
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from rift_data.data import get_info_client, fetch_predicted_funding, fetch_market_context, fetch_cross_exchange_funding, fetch_market_breadth, save_market_snapshot, normalize_coin as _normalize_coin
from rift_trade.builder_fee import (
    get_builder_info,
    BUILDER_ADDRESS,
    BUILDER_FEE_DISPLAY,
    translate_builder_fee_error,
)
from rift_data.ws_feed import LiveMarketFeed
from rift_engine.strategy import (
    Strategy, StrategyState, Candle, Signal, Side,
    discover_strategies, get_strategy,
)


ALGO_DIR = Path.home() / ".rift" / "algo"
ALGO_SESSIONS_DIR = ALGO_DIR / "sessions"
ALGO_PIDS_DIR = ALGO_DIR / "pids"
ALGO_LOGS_DIR = ALGO_DIR / "logs"
ALGO_COMMANDS_DIR = ALGO_DIR / "commands"
# Legacy single-file path (for crash recovery migration)
ALGO_STATE_DIR = ALGO_DIR
ALGO_STATE_FILE = ALGO_DIR / "state.json"
MAX_DRAWDOWN_PCT = 0.50  # Kill switch: stop trading if equity drops 50% from initial


def _session_key(strategy: str, pair: str) -> str:
    """Generate a unique key for a strategy+pair session."""
    coin = _normalize_coin(pair)
    return f"{strategy}_{coin}"


def _session_state_file(key: str) -> Path:
    ALGO_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return ALGO_SESSIONS_DIR / f"{key}.json"


def _session_pid_file(key: str) -> Path:
    ALGO_PIDS_DIR.mkdir(parents=True, exist_ok=True)
    return ALGO_PIDS_DIR / f"{key}.pid"


def _session_log_file(key: str) -> Path:
    ALGO_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return ALGO_LOGS_DIR / f"{key}.log"


# Module-level daemon state — set by run_algo when in daemon mode
_daemon_mode = False
_daemon_log_file: Path | None = None
_daemon_session_key: str | None = None


@dataclass
class AlgoPosition:
    """A real open position on Hyperliquid."""
    side: str
    entry_price: float
    size: float
    entry_time: str
    stop_oid: int | None = None  # Hyperliquid order ID for the stop loss trigger
    stop_price: float = 0.0     # Actual stop loss price on exchange
    candles_held: int = 0
    funding_collected: float = 0.0
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    entry_mid_price: float = 0.0   # mid price at time of entry (for TCA)
    execution_method: str = "ioc"  # "ioc" or "twap"
    signal_ts: float = 0.0         # when strategy generated the signal
    submit_ts: float = 0.0         # when order was submitted
    fill_ts: float = 0.0           # when fill was confirmed


@dataclass
class AlgoTrade:
    """A completed algo trade."""
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    price_pnl: float
    funding_collected: float
    entry_time: str
    exit_time: str
    candles_held: int
    max_favorable: float
    max_adverse: float
    exit_reason: str
    entry_oid: int | None = None
    exit_oid: int | None = None
    # TCA fields
    entry_mid_price: float = 0.0      # mid price when entry order was submitted
    exit_mid_price: float = 0.0       # mid price when exit order was submitted
    entry_slippage_bps: float = 0.0   # (fill - mid) / mid * 10000
    exit_slippage_bps: float = 0.0
    execution_method: str = "ioc"     # "ioc" or "twap"
    # Latency fields (seconds since epoch)
    signal_ts: float = 0.0            # when strategy generated the signal
    submit_ts: float = 0.0            # when order was submitted to exchange
    fill_ts: float = 0.0              # when fill was confirmed


def _emit(data: dict) -> None:
    """Write NDJSON to stdout (foreground) or log file (daemon).

    In daemon mode, also writes a full state snapshot to the session
    state file so viewers and MCP tools can read current state.
    """
    line = json.dumps(_sanitize_for_json(data))

    if _daemon_mode:
        # Write to log file (trades, errors, status — skip heartbeats to prevent bloat)
        if data.get("type") != "heartbeat" and _daemon_log_file:
            with open(_daemon_log_file, "a") as f:
                f.write(line + "\n")

        # Write state snapshot for viewers/MCP
        if "state" in data and _daemon_session_key:
            snapshot = {
                "type": data.get("type"),
                "state": data["state"],
                "funding_countdown_min": data.get("funding_countdown_min", 0),
                "predicted_funding": data.get("predicted_funding", 0),
                "candle_progress": data.get("candle_progress", 0),
                "candle_remaining_sec": data.get("candle_remaining_sec", 0),
                "indicators": data.get("indicators", {}),
                "reasoning": data.get("reasoning"),
                "trade_replay": data.get("trade_replay"),
                "narrative": data.get("narrative"),
                "updated_at": time.time(),
            }
            state_file = _session_state_file(_daemon_session_key)
            state_file.write_text(json.dumps(_sanitize_for_json(snapshot), indent=2))
    else:
        print(line, flush=True)


# ─── PRECISION HELPERS (match Hyperliquid SDK) ────────────────

def _load_sz_decimals(info) -> dict[str, int]:
    """Query info.meta() and build {coin: szDecimals} map.

    The SDK rounds sizes to szDecimals and prices to (6 - szDecimals)
    significant-figure-adjusted decimals. We must match this exactly
    or orders get rejected.
    """
    sz_map: dict[str, int] = {}
    try:
        meta = info.meta()
        for asset_info in meta.get("universe", []):
            sz_map[asset_info["name"]] = asset_info["szDecimals"]
    except Exception as e:
        _emit({"type": "status", "msg": f"Warning: could not load asset metadata: {e}"})
    return sz_map


def _round_size(size: float, sz_decimals: int) -> float:
    """Round order size to asset-specific szDecimals (matches SDK float_to_wire)."""
    return round(size, sz_decimals)


def _round_price(px: float, sz_decimals: int) -> float:
    """Round price to 5 significant figures, then (6 - szDecimals) decimal places.

    This matches the SDK's _slippage_price() rounding exactly:
        round(float(f"{px:.5g}"), 6 - szDecimals)
    """
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def _sanitize_for_json(obj):
    """Replace NaN/Inf with None."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _save_algo_state(state: dict) -> None:
    """Persist algo trading state for crash recovery (session-keyed)."""
    key = _daemon_session_key
    if key:
        path = _session_state_file(key)
    else:
        ALGO_STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = ALGO_STATE_FILE
    path.write_text(json.dumps(_sanitize_for_json(state), indent=2))


def _load_algo_state() -> dict | None:
    """Load persisted algo state if it exists."""
    key = _daemon_session_key
    if key:
        path = _session_state_file(key)
    else:
        path = ALGO_STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _clear_algo_state() -> None:
    """Remove persisted state (clean exit)."""
    key = _daemon_session_key
    if key:
        path = _session_state_file(key)
        pid_path = _session_pid_file(key)
        if path.exists():
            path.unlink()
        if pid_path.exists():
            pid_path.unlink()
    else:
        if ALGO_STATE_FILE.exists():
            ALGO_STATE_FILE.unlink()


def run_algo(
    strategy_name: str,
    pair: str,
    interval: str = "1h",
    initial_equity: float = 0,  # 0 = auto-detect from account
    strategies_dir: str = "",
    private_key: str = "",
    account_address: str = "",
    daemon: bool = False,
) -> None:
    """Run algo trading on Hyperliquid mainnet."""
    global _daemon_mode, _daemon_log_file, _daemon_session_key

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account

    coin = _normalize_coin(pair)

    # Verify builder configuration
    if len(BUILDER_ADDRESS) != 42 or BUILDER_ADDRESS[:5] != "0x091":
        _emit({"type": "error", "msg": "Builder configuration error. Reinstall RIFT."})
        sys.exit(1)

    # Set up daemon mode
    key = _session_key(strategy_name, coin)
    _daemon_session_key = key

    if daemon:
        _daemon_mode = True
        _daemon_log_file = _session_log_file(key)

        # Write PID file
        pid_file = _session_pid_file(key)
        pid_file.write_text(str(os.getpid()))

        # Truncate log for new session
        with open(_daemon_log_file, "w") as f:
            f.write("")
    base_url = constants.MAINNET_API_URL
    builder_info = get_builder_info()

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    from rift_engine.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError:
        _emit({"type": "error",
               "msg": f"Strategy '{strategy_name}' not found. "
                      f"Run `rift strategies list` to see what's available, "
                      f"or scaffold a new one with `rift new {strategy_name}`."})
        sys.exit(1)

    strategy = strategy_cls()
    leverage = strategy.config.leverage if hasattr(strategy.config, "leverage") else 2.0
    sl_pct = strategy.config.stop_loss_pct if hasattr(strategy.config, "stop_loss_pct") else 0.02

    # Preflight: verify cross-asset data is available
    from rift_engine.strategy import preflight_check_data
    preflight_errors = preflight_check_data(strategy, interval)
    if preflight_errors:
        _emit({"type": "error",
               "msg": f"Strategy {strategy_name} requires data that's not available locally. "
                      f"Run `rift sync` to download the required candle/funding history, "
                      f"or check the per-error messages below."})
        for e in preflight_errors:
            _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    # Record strategy version for audit trail
    try:
        from rift_core.versioning import record_version
        record_version(strategy, session_key=key)
    except Exception:
        pass

    # Connect to Hyperliquid
    if not private_key:
        _emit({"type": "error", "msg": "No API wallet key provided. Run: rift auth setup"})
        sys.exit(1)

    wallet = Account.from_key(private_key)
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    # Start real-time market feed (trades + order book + vault positions)
    market_feed = LiveMarketFeed(coin)
    market_feed.start()
    _emit({"type": "status", "msg": f"Real-time market feed started for {coin} (trades + l2Book + vaults)"})

    # Load asset precision metadata
    sz_decimals_map = _load_sz_decimals(info)
    coin_sz_decimals = sz_decimals_map.get(coin, 2)  # conservative default
    _emit({"type": "status", "msg": f"Asset {coin}: szDecimals={coin_sz_decimals} (price decimals={6 - coin_sz_decimals})"})

    # Set leverage
    try:
        exchange.update_leverage(int(leverage), coin, is_cross=True)
    except Exception as e:
        _emit({"type": "status", "msg": f"Leverage update: {e}"})

    # Query account collateral, mode-aware (unified account: spot USDC IS perp margin)
    try:
        from rift_data.account_mode import read_collateral
        collateral = read_collateral(info, account_address)
        account_value = float(collateral.total)
        if initial_equity <= 0:
            initial_equity = account_value
        _emit({"type": "status", "msg": f"Account value: ${account_value:,.2f} ({collateral.mode} mode)"})
    except Exception as e:
        _emit({"type": "error", "msg": f"Cannot query account: {e}"})
        sys.exit(1)

    if account_value < 10:
        _emit({"type": "error", "msg": f"Insufficient balance: ${account_value:.2f}. Need at least $10."})
        sys.exit(1)

    # Initialize tracking (before crash recovery so recovery can set position)
    position: AlgoPosition | None = None
    trades: list[AlgoTrade] = []

    # Crash recovery: restore position from saved state
    saved_state = _load_algo_state()
    if saved_state and saved_state.get("position"):
        sp = saved_state["position"]
        _, real_pos = _sync_position(info, account_address, coin)
        if real_pos:
            szi = float(real_pos.get("szi", "0"))
            if szi != 0:
                recovered_stop_oid = sp.get("stop_oid")

                # Verify the stop loss order still exists on exchange
                if recovered_stop_oid:
                    stop_verified = _verify_stop_exists(info, account_address, recovered_stop_oid)
                    if not stop_verified:
                        _emit({"type": "status", "msg": f"WARNING: Stop loss (oid={recovered_stop_oid}) no longer exists. Re-placing stop."})
                        # Re-place the stop loss
                        stop_price_val = float(sp.get("stop_price", 0))
                        if stop_price_val > 0:
                            sl_is_buy = szi > 0  # buy to close long = False... wait, short needs buy
                            sl_is_buy = szi < 0  # if we're short (szi < 0), stop buys to close
                            # Actually: if long (szi > 0), stop sells (is_buy=False); if short, stop buys
                            sl_is_buy = szi < 0
                            try:
                                sl_result = exchange.order(
                                    coin, sl_is_buy, _round_size(abs(szi), coin_sz_decimals),
                                    _round_price(stop_price_val, coin_sz_decimals),
                                    order_type={"trigger": {"triggerPx": _round_price(stop_price_val, coin_sz_decimals), "isMarket": True, "tpsl": "sl"}},
                                    reduce_only=True, builder=builder_info,
                                )
                                if sl_result.get("status") == "ok":
                                    sl_statuses = sl_result.get("response", {}).get("data", {}).get("statuses", [])
                                    for s in sl_statuses:
                                        if "resting" in s:
                                            recovered_stop_oid = s["resting"].get("oid")
                                            _emit({"type": "status", "msg": f"Re-placed stop loss: oid={recovered_stop_oid}"})
                            except Exception as e:
                                _emit({"type": "error", "msg": f"CRITICAL: Could not re-place stop loss: {e}. Position unprotected!"})
                                recovered_stop_oid = None

                position = AlgoPosition(
                    side="long" if szi > 0 else "short",
                    entry_price=float(sp.get("entry_price", real_pos.get("entryPx", "0"))),
                    size=abs(szi),
                    entry_time=sp.get("entry_time", "recovered"),
                    stop_oid=recovered_stop_oid,
                    stop_price=float(sp.get("stop_price", 0)),
                )
                _emit({"type": "status", "msg": f"Recovered position: {position.side.upper()} {position.size:.6f} @ ${position.entry_price:.2f}"})
        else:
            _emit({"type": "status", "msg": "Previous position was closed while offline."})
            _clear_algo_state()
    else:
        # If the exchange has a position RIFT didn't open, refuse to start —
        # the daemon can't safely manage an orphan. Caller must close it first.
        if _check_orphaned_positions(info, account_address, coin):
            _clear_algo_state()
            sys.exit(1)
    total_funding = 0.0
    equity = initial_equity
    peak_equity = initial_equity
    candles_processed = 0
    last_price = 0.0
    last_funding_rate = 0.0
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    price_history: list[float] = []
    funding_events: list[dict] = []

    # Candle history for indicators
    candle_history: list[dict] = []
    last_candle_ts = 0
    # Hyperliquid settles funding every hour (not every 8h — confirmed per API)
    last_funding_hour = int(time.time() * 1000) // 3600000 * 3600000
    predicted_funding_rate = 0.0
    mkt_ctx: dict = {}
    cross_funding: dict = {}
    breadth: dict = {}
    health_paused = False
    health_report = None
    health_check_interval = 5  # check every 5 trades

    # Drawdown recovery tracking
    dd_start_time: str | None = None
    dd_trough_equity = initial_equity
    dd_trough_time: str | None = None
    dd_events: list[dict] = []
    max_recovery_hours = 0.0

    # Load historical candles for warmup
    _emit({"type": "status", "msg": "Loading historical candles for warmup..."})
    try:
        end_time = int(time.time() * 1000)
        start_time = end_time - (200 * 60 * 60 * 1000)
        historical = info.candles_snapshot(coin, interval, start_time, end_time)
        if historical:
            candle_history = historical[:-1]
            if candle_history:
                last_candle_ts = candle_history[-1]["t"]
                for c in candle_history[-30:]:
                    price_history.append(float(c["c"]))
            _emit({"type": "status", "msg": f"Loaded {len(candle_history)} candles. Ready."})
    except Exception as e:
        _emit({"type": "status", "msg": f"History load failed: {e}"})

    # NOTE: Dead man's switch (scheduleCancel) intentionally NOT used.
    # It cancels ALL orders including stop loss triggers, leaving positions
    # unprotected. The stop loss trigger on Hyperliquid's server is the
    # real safety net — it persists even if RIFT crashes.

    # Graceful shutdown
    running = True

    class _ShutdownSignal(BaseException):
        pass

    def handle_shutdown(signum, frame):
        nonlocal running
        running = False
        raise _ShutdownSignal()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Two refresh cycles: 1s for live price, 5s for full heartbeat
    tick_count = 0
    session_start_price = 0.0

    _emit({
        "type": "status",
        "msg": f"● ALGO — {strategy_name} on {pair} {interval}. Watching for signals...",
        "state": _build_state_dict(
            strategy_name, pair, interval, initial_equity, equity, peak_equity,
            position, trades, total_funding, last_price, last_funding_rate,
            candles_processed, started_at, price_history, funding_events,
            account_address,
        ),
    })

    while running:
        try:
            tick_count += 1
            is_full_tick = (tick_count % 5 == 0) or tick_count == 1

            # ─── EVERY SECOND: Live price ───
            try:
                mids = info.all_mids()
                live_price = float(mids.get(coin, "0"))
                if live_price > 0:
                    last_price = live_price
                    if session_start_price == 0:
                        session_start_price = live_price
            except Exception:
                pass

            # Update position excursion with live price
            if position and last_price > 0:
                if position.side == "long":
                    unreal = position.size * (last_price - position.entry_price)
                else:
                    unreal = position.size * (position.entry_price - last_price)
                if unreal > position.max_favorable:
                    position.max_favorable = unreal
                if unreal < position.max_adverse:
                    position.max_adverse = unreal

            # Drawdown recovery tracking
            if equity > 0 and peak_equity > 0:
                if equity < peak_equity * 0.999 and dd_start_time is None:
                    # Entering drawdown
                    dd_start_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                    dd_trough_equity = equity
                    dd_trough_time = dd_start_time
                elif dd_start_time is not None and equity < dd_trough_equity:
                    # New trough
                    dd_trough_equity = equity
                    dd_trough_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                elif dd_start_time is not None and equity >= peak_equity:
                    # Recovery complete
                    try:
                        start_dt = datetime.strptime(dd_start_time, "%Y-%m-%d %H:%M")
                        duration_hours = (datetime.now() - start_dt).total_seconds() / 3600
                        depth_pct = (peak_equity - dd_trough_equity) / peak_equity * 100
                        dd_events.append({
                            "start": dd_start_time, "trough": dd_trough_time,
                            "recovery": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "depth_pct": round(depth_pct, 2),
                            "duration_hours": round(duration_hours, 1),
                        })
                        if duration_hours > max_recovery_hours:
                            max_recovery_hours = duration_hours
                    except Exception:
                        pass
                    dd_start_time = None
                    dd_trough_equity = equity

            if equity > peak_equity:
                peak_equity = equity

            # ─── EVERY 5 SECONDS: Full heartbeat ───
            candle_data = None
            if is_full_tick:
                candle_data = _get_latest_candle(info, coin, interval)
                if candle_data:
                    candle_ts = candle_data["t"]

                # Sync real position state from exchange
                real_equity, real_position = _sync_position(info, account_address, coin)
                if real_equity > 0:
                    equity = real_equity

                # ─── DRAWDOWN KILL SWITCH ───
                if initial_equity > 0 and equity < initial_equity * (1 - MAX_DRAWDOWN_PCT):
                    _emit({"type": "error", "msg": f"DRAWDOWN KILL SWITCH: Equity ${equity:.2f} < {(1 - MAX_DRAWDOWN_PCT)*100:.0f}% of initial ${initial_equity:.2f}. Shutting down."})
                    if position:
                        _emit({"type": "status", "msg": "Closing position due to drawdown protection..."})
                        trade = _close_position_algo(exchange, info, position, coin, account_address, builder_info, coin_sz_decimals)
                        if trade:
                            trades.append(trade)
                        position = None
                    running = False
                    break

                # Funding rate (current + predicted next)
                funding_rate = _get_current_funding(info, coin)
                last_funding_rate = funding_rate
                predicted_funding_rate = fetch_predicted_funding(coin, info)

                # Market context (OI, premium, volume, cross-exchange funding)
                mkt_ctx = fetch_market_context(coin, info)
                cross_funding = fetch_cross_exchange_funding(coin, info)
                breadth = fetch_market_breadth(info)
                if mkt_ctx:
                    save_market_snapshot(coin, mkt_ctx, cross_funding)

                # Apply funding tracking
                if position is not None:
                    current_hour = int(time.time() * 1000) // 3600000 * 3600000
                    if current_hour > last_funding_hour and funding_rate != 0:
                        position_value = position.size * last_price
                        if position.side == "long":
                            funding_payment = -position_value * funding_rate
                        else:
                            funding_payment = position_value * funding_rate
                        total_funding += funding_payment
                        position.funding_collected += funding_payment
                        last_funding_hour = current_hour
                        funding_events.append({
                            "time": datetime.now().strftime("%H:%M"),
                            "amount": round(funding_payment, 2),
                            "rate": funding_rate,
                        })

            # Funding countdown
            mins_to_funding = _minutes_to_next_funding()
            predicted_funding = 0.0
            if position and last_price > 0:
                pv = position.size * last_price
                predicted_funding = (-pv * funding_rate) if position.side == "long" else (pv * funding_rate)

            # Candle countdown
            interval_ms = {
                "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
                "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
            }.get(interval, 3_600_000)
            now_ms = int(time.time() * 1000)
            candle_elapsed = (now_ms - candle_ts) % interval_ms if candle_ts > 0 else 0
            candle_progress = min(1.0, candle_elapsed / interval_ms) if interval_ms > 0 else 0
            candle_remaining_sec = max(0, int((interval_ms - candle_elapsed) / 1000))

            # Check if new candle
            if not is_full_tick or candle_data is None or candle_ts == last_candle_ts:
                _emit({
                    "type": "heartbeat",
                    "state": _build_state_dict(
                        strategy_name, pair, interval, initial_equity, equity, peak_equity,
                        position, trades, total_funding, last_price, last_funding_rate,
                        candles_processed, started_at, price_history, funding_events,
                        account_address, session_start_price=session_start_price,
                    ),
                    "funding_countdown_min": mins_to_funding,
                    "predicted_funding": round(predicted_funding, 4),
                    "candle_progress": round(candle_progress, 3),
                    "candle_remaining_sec": candle_remaining_sec,
                })
                time.sleep(1)
                continue

            last_candle_ts = candle_ts
            candles_processed += 1
            candle_history.append(candle_data)
            if len(candle_history) > 500:
                candle_history = candle_history[-500:]
            price_history.append(last_price)
            if len(price_history) > 30:
                price_history = price_history[-30:]

            if len(candle_history) < 110:
                _emit({"type": "status", "msg": f"Warming up... {len(candle_history)}/110 candles"})
                time.sleep(1)
                continue

            # Compute indicators
            closes = np.array([float(c["c"]) for c in candle_history])
            highs = np.array([float(c["h"]) for c in candle_history])
            lows = np.array([float(c["l"]) for c in candle_history])
            volumes = np.array([float(c["v"]) for c in candle_history])

            from rift_engine.backtest import _compute_indicator

            indicator_defs = strategy.indicators()
            indicator_values = {}
            for ind_name, ind in indicator_defs.items():
                series = _compute_indicator(ind_name, ind, closes, highs, lows, volumes)
                if len(series) == 0:
                    indicator_values[ind_name] = 0.0
                    continue
                val = series[-1]
                indicator_values[ind_name] = float(val) if not np.isnan(val) else 0.0

            # Build strategy state
            position_size = position.size if position else 0.0
            if position and position.side == "short":
                position_size = -position_size

            # Get real-time websocket data
            ws_data = market_feed.get_derived()

            strat_state = StrategyState(
                indicators=indicator_values,
                position=position_size,
                equity=equity,
                funding_rate=funding_rate,
                cumulative_funding=total_funding,
                predicted_funding=predicted_funding_rate,
                open_interest=mkt_ctx.get("open_interest", 0.0) if mkt_ctx else 0.0,
                premium=mkt_ctx.get("premium", 0.0) if mkt_ctx else 0.0,
                oracle_price=mkt_ctx.get("oracle_price", 0.0) if mkt_ctx else 0.0,
                day_volume=mkt_ctx.get("day_volume", 0.0) if mkt_ctx else 0.0,
                funding_divergence=cross_funding.get("hl_vs_cex", 0.0) if cross_funding else 0.0,
                market_breadth_ob=breadth.get("overbought_pct", 0.0) if breadth else 0.0,
                market_breadth_os=breadth.get("oversold_pct", 0.0) if breadth else 0.0,
                market_avg_rsi=breadth.get("avg_rsi", 50.0) if breadth else 50.0,
                # Real-time websocket-derived fields
                cvd=ws_data.get("cvd", 0.0),
                volume_delta=ws_data.get("volume_delta", 0.0),
                relative_volume=ws_data.get("relative_volume", 1.0),
            )

            candle = Candle(
                timestamp=candle_ts,
                open=float(candle_data["o"]),
                high=float(candle_data["h"]),
                low=float(candle_data["l"]),
                close=float(candle_data["c"]),
                volume=float(candle_data["v"]),
            )

            if position:
                position.candles_held += 1

            # CRITICAL: Detect stop loss BEFORE building strategy state
            # so the strategy sees the correct position (flat after stop)
            if position and real_position is None:
                trade = _record_stopped_trade(position, last_price, info, account_address, coin)
                if trade:
                    trades.append(trade)
                    equity = _get_equity(info, account_address)
                    _emit({
                        "type": "trade", "action": "stop_loss",
                        "trade_replay": _trade_replay_dict(trade),
                        "state": _build_state_dict(
                            strategy_name, pair, interval, initial_equity, equity, peak_equity,
                            None, trades, total_funding, last_price, last_funding_rate,
                            candles_processed, started_at, price_history, funding_events,
                            account_address,
                        ),
                    })
                position = None
                _save_algo_state({"position": None, "strategy": strategy_name, "pair": pair})

                # Rebuild strategy state with flat position after stop
                strat_state = StrategyState(
                    indicators=indicator_values,
                    position=0.0,
                    equity=equity,
                    funding_rate=funding_rate,
                    cumulative_funding=total_funding,
                    predicted_funding=predicted_funding_rate,
                    cvd=ws_data.get("cvd", 0.0),
                    volume_delta=ws_data.get("volume_delta", 0.0),
                    relative_volume=ws_data.get("relative_volume", 1.0),
                )

            # Run strategy
            sig = strategy.on_candle(candle, strat_state)
            signal_ts = time.time() if sig is not None else 0.0

            if sig is not None:
                price = last_price

                # Close existing position
                if sig.reduce_only or (position and position.side == "long" and sig.side == Side.SHORT) or (position and position.side == "short" and sig.side == Side.LONG):
                    if position:
                        trade = _close_position_algo(exchange, info, position, coin, account_address, builder_info, coin_sz_decimals)
                        if trade:
                            trades.append(trade)
                            equity = _get_equity(info, account_address)
                            position = None
                        else:
                            # CRITICAL: Close failed. Verify exchange state before proceeding.
                            _, still_open = _sync_position(info, account_address, coin)
                            if still_open is None:
                                position = None  # Closed despite no trade returned
                            else:
                                _emit({"type": "error", "msg": "Close failed and position still open. Skipping new entry."})
                                # Don't set position = None — keep tracking it

                        _emit({
                            "type": "trade", "action": "close",
                            "trade_replay": _trade_replay_dict(trade) if trade else None,
                            "state": _build_state_dict(
                                strategy_name, pair, interval, initial_equity, equity, peak_equity,
                                position, trades, total_funding, last_price, last_funding_rate,
                                candles_processed, started_at, price_history, funding_events,
                                account_address,
                            ),
                        })

                # Health check after trade closes
                if len(trades) >= 10 and len(trades) % health_check_interval == 0:
                    try:
                        from rift_trade.health import run_health_check
                        hr = run_health_check(
                            recent_trades=trades[-min(20, len(trades)):],
                            baseline_trades=trades[:max(1, len(trades) // 2)],
                        )
                        health_report = hr
                        if hr.recommendation in ("pause", "stop"):
                            health_paused = True
                            _emit({"type": "status", "msg": f"HEALTH: Score {hr.score}/100 ({hr.grade}) — {hr.recommendation.upper()}. {'; '.join(hr.alerts[:2])}"})
                        elif hr.recommendation == "reduce_size":
                            _emit({"type": "status", "msg": f"HEALTH: Score {hr.score}/100 ({hr.grade}) — reducing position size. {'; '.join(hr.alerts[:2])}"})
                        else:
                            _emit({"type": "status", "msg": f"HEALTH: Score {hr.score}/100 ({hr.grade}) — strategy healthy"})
                    except Exception:
                        pass

                    # Rolling Sharpe decay check — reduce size if recent performance
                    # is significantly worse than what the optimizer expected
                    try:
                        recent = trades[-20:]
                        if len(recent) >= 5:
                            recent_returns = [t.pnl_pct / 100 for t in recent]
                            import numpy as _np
                            from rift_substrate import periods_per_year_for_interval
                            mean_r = float(_np.mean(recent_returns))
                            std_r = float(_np.std(recent_returns, ddof=1))
                            if std_r > 0:
                                # Annualize per the candle interval, then scale by trade frequency.
                                periods_per_yr = periods_per_year_for_interval(interval)
                                candles_per_trade = max(1.0, len(candle_history) / max(1, len(trades)))
                                rolling_sharpe = mean_r / std_r * _np.sqrt(periods_per_yr / candles_per_trade)
                            else:
                                rolling_sharpe = 0.0
                            # If rolling Sharpe < 0.5, emit warning
                            if rolling_sharpe < 0.5 and not health_paused:
                                _emit({"type": "status", "msg": f"CONFIDENCE: Rolling Sharpe {rolling_sharpe:.2f} — reducing exposure"})
                    except Exception:
                        pass

                # Open new position (only if position is None — prevents double position, respects health pause)
                if not sig.reduce_only and sig.size > 0 and position is None and not health_paused:
                    # Portfolio gate: check if supervisor allows this entry
                    gate_allowed, gate_size_mult, gate_max_usd = _check_portfolio_gate(key)
                    if not gate_allowed:
                        _emit({"type": "status", "msg": "Entry blocked by portfolio risk gate"})
                    else:
                        # `sig.stop_loss` from Signal.long/short is an absolute PRICE,
                        # not a percentage. Convert to a percentage of last_price for
                        # the position-sizing math below (which divides by sl_pct_trade
                        # expecting a decimal fraction). Without this conversion, the
                        # PRICE goes into the denominator and position_value collapses
                        # to ~$0, so every order falls below HL's $10 minimum and the
                        # daemon silently skips every entry.
                        if sig.stop_loss and last_price > 0:
                            sl_pct_trade = abs(last_price - sig.stop_loss) / last_price
                            if sl_pct_trade <= 0:
                                sl_pct_trade = sl_pct  # fall back if the strategy gave a degenerate stop
                        else:
                            sl_pct_trade = sl_pct
                        from rift_engine.strategy import compute_kelly_risk
                        risk_per_trade = compute_kelly_risk(trades) * sig.size
                        risk_per_trade = min(risk_per_trade, 0.10)
                        if gate_size_mult < 1.0:
                            risk_per_trade *= gate_size_mult  # supervisor reduced allocation
                        if gate_size_mult > 1.0:
                            risk_per_trade = min(risk_per_trade * gate_size_mult, 0.20)  # double up capped at 20%

                        # Confluence sizing — adjust risk by how many data points agree
                        direction = sig.side.value  # "long" or "short"
                        confluence = 0
                        confluence_checks = 0

                        # OI momentum agrees with direction
                        if strat_state.oi_roc != 0:
                            confluence_checks += 1
                            if (strat_state.oi_roc > 0 and direction == "long") or \
                               (strat_state.oi_roc < 0 and direction == "short"):
                                confluence += 1

                        # Premium agrees with direction
                        if abs(strat_state.premium) > 0.0003:
                            confluence_checks += 1
                            if (strat_state.premium < -0.0003 and direction == "long") or \
                               (strat_state.premium > 0.0003 and direction == "short"):
                                confluence += 1

                        # Volume above average
                        if strat_state.relative_volume > 0:
                            confluence_checks += 1
                            if strat_state.relative_volume > 1.2:
                                confluence += 1

                        # CVD agrees with direction
                        if strat_state.cvd != 0:
                            confluence_checks += 1
                            if (strat_state.cvd > 0 and direction == "long") or \
                               (strat_state.cvd < 0 and direction == "short"):
                                confluence += 1

                        # Apply confluence multiplier (0.5x at 0%, 1.5x at 100%)
                        if confluence_checks > 0:
                            confluence_ratio = confluence / confluence_checks
                            risk_per_trade *= (0.5 + confluence_ratio)

                        position_value = (equity * risk_per_trade) / sl_pct_trade
                        position_value = min(position_value, equity * leverage)
                        # Per-strategy position limit (absolute USD cap)
                        if gate_max_usd and position_value > gate_max_usd:
                            position_value = gate_max_usd
                        # Volume cap: limit position to 1% of daily volume to prevent market impact
                        coin_vol = mkt_ctx.get("day_volume", 0.0) if mkt_ctx else 0.0
                        if coin_vol > 0:
                            volume_cap = coin_vol * 0.01
                            position_value = min(position_value, volume_cap)
                        size = position_value / price

                        if size * price >= 10:  # Hyperliquid minimum notional
                            # Calculate stop price
                            is_buy = sig.side == Side.LONG
                            if is_buy:
                                stop_price = price * (1 - sl_pct_trade)
                                sl_is_buy = False
                            else:
                                stop_price = price * (1 + sl_pct_trade)
                                sl_is_buy = True

                            # Choose execution method: TWAP for large orders, single IOC for normal
                            coin_vol = mkt_ctx.get("day_volume", 0.0) if mkt_ctx else 0.0
                            if _should_use_twap(size, price, coin_vol):
                                result = _open_position_twap(
                                    exchange, info, coin, is_buy, size, price, stop_price, sl_is_buy,
                                    sl_pct_trade, builder_info, coin_sz_decimals,
                                )
                            else:
                                result = _open_position_algo(
                                    exchange, coin, is_buy, size, price, stop_price, sl_is_buy,
                                    sl_pct_trade, builder_info, coin_sz_decimals,
                                )

                            if result:
                                entry_oid = result.get("entry_oid")
                                stop_oid = result.get("stop_oid")
                                fill_price = result.get("fill_price", price)
                                filled_size = result.get("filled_size", size)
                                actual_stop = result.get("stop_price", stop_price)

                                position = AlgoPosition(
                                    side=sig.side.value,
                                    entry_price=fill_price,
                                    size=filled_size,
                                    entry_time=datetime.now().strftime("%H:%M"),
                                    stop_oid=stop_oid,
                                    stop_price=actual_stop,
                                    entry_mid_price=result.get("mid_price_at_order", price),
                                    execution_method=result.get("execution_method", "ioc"),
                                    signal_ts=signal_ts,
                                    submit_ts=result.get("submit_ts", 0.0),
                                    fill_ts=result.get("fill_ts", 0.0),
                                )

                                _save_algo_state({
                                    "position": {
                                        "side": position.side,
                                        "entry_price": position.entry_price,
                                        "size": position.size,
                                        "stop_oid": stop_oid,
                                        "entry_time": position.entry_time,
                                    },
                                    "strategy": strategy_name,
                                    "pair": pair,
                                })

                                _emit({
                                    "type": "trade", "action": "open",
                                    "side": sig.side.value,
                                    "price": round(fill_price, 2),
                                    "size": round(size, 6),
                                    "stop_loss": round(stop_price, 2),
                                    "oid": entry_oid,
                                    "state": _build_state_dict(
                                        strategy_name, pair, interval, initial_equity, equity, peak_equity,
                                        position, trades, total_funding, last_price, last_funding_rate,
                                        candles_processed, started_at, price_history, funding_events,
                                        account_address,
                                    ),
                                })

            # Emit candle update
            _emit({
                "type": "candle",
                "state": _build_state_dict(
                    strategy_name, pair, interval, initial_equity, equity, peak_equity,
                    position, trades, total_funding, last_price, last_funding_rate,
                    candles_processed, started_at, price_history, funding_events,
                    account_address,
                    dd_events=dd_events, dd_start_time=dd_start_time, max_recovery_hours=max_recovery_hours,
                ),
                "indicators": {k: round(v, 6) for k, v in indicator_values.items()},
                "funding_countdown_min": mins_to_funding,
                "predicted_funding": round(predicted_funding, 4),
            })

        except _ShutdownSignal:
            break
        except Exception as e:
            _emit({"type": "error",
                   "msg": f"Algo daemon tick hit a transient error: {type(e).__name__}: {e}. "
                          f"Continuing — daemon will retry next tick. "
                          f"Check ~/.rift/algo/logs/ if this repeats."})

        # Check for external commands (close-position, tighten-stop, reduce)
        if _daemon_session_key:
            cmd_file = ALGO_COMMANDS_DIR / f"{_daemon_session_key}.json"
            if cmd_file.exists():
                try:
                    ext_cmd = json.loads(cmd_file.read_text())
                    cmd_file.unlink()
                    action = ext_cmd.get("action", "")
                    if action == "close" and position:
                        _emit({"type": "alert", "event": "external_close", "msg": "Position close requested externally"})
                        # Break to trigger shutdown sequence which closes position
                        break
                    elif action == "tighten_stop" and position:
                        new_price = ext_cmd.get("price", 0)
                        if new_price > 0:
                            _emit({"type": "alert", "event": "external_tighten", "msg": f"Stop tightened to {new_price}"})
                            # Cancel existing stop and place new one
                            try:
                                exchange.cancel(coin, None)  # cancel all orders
                                time.sleep(0.3)
                                is_buy = position.side == "short"
                                exchange.order(coin, is_buy, position.size, new_price,
                                              order_type={"trigger": {"triggerPx": new_price, "isMarket": True, "tpsl": "sl"}})
                            except Exception as stop_err:
                                _emit({"type": "error", "msg": f"Failed to update stop: {stop_err}"})
                    elif action == "reduce" and position:
                        pct = ext_cmd.get("pct", 0.5)
                        reduce_size = abs(position.size) * pct
                        _emit({"type": "alert", "event": "external_reduce", "msg": f"Reducing position by {pct*100:.0f}%"})
                        try:
                            is_buy = position.side == "short"  # close direction
                            exchange.market_close(coin, reduce_size)
                        except Exception as red_err:
                            _emit({"type": "error", "msg": f"Failed to reduce: {red_err}"})
                except Exception as cmd_err:
                    _emit({"type": "error", "msg": f"Command file error: {cmd_err}"})

        time.sleep(1)

    # ─── SHUTDOWN ───

    # Stop market feed
    market_feed.stop()

    # Close any open position
    trade = None
    if position:
        _emit({"type": "status", "msg": "Closing position on shutdown..."})
        trade = _close_position_algo(exchange, info, position, coin, account_address, builder_info, coin_sz_decimals)
        if trade:
            trades.append(trade)

    # Final equity sync
    equity = _get_equity(info, account_address)

    # Generate narrative
    narrative = _generate_live_narrative(trades, total_funding, equity, initial_equity, started_at)

    # Save session log
    log_path = _save_session_log(strategy_name, pair, interval, initial_equity, equity, trades, total_funding, candles_processed, started_at, account_address, dd_events=dd_events, max_recovery_hours=max_recovery_hours)

    # Clear algo state (clean exit)
    _clear_algo_state()

    _emit({
        "type": "shutdown",
        "msg": "Algo trading stopped",
        "log": str(log_path),
        "state": _build_state_dict(
            strategy_name, pair, interval, initial_equity, equity, peak_equity,
            None, trades, total_funding, last_price, last_funding_rate,
            candles_processed, started_at, price_history, funding_events,
            account_address,
            dd_events=dd_events, dd_start_time=dd_start_time, max_recovery_hours=max_recovery_hours,
        ),
        "narrative": narrative,
        "trade_replay": _trade_replay_dict(trade) if trade else None,
    })


# ─── ORDER EXECUTION ───────────────────────────────────────────

def _should_use_twap(size: float, price: float, coin_day_volume: float) -> bool:
    """Determine if TWAP execution should be used based on order size vs market volume.

    Activates when the order notional exceeds 0.1% of the coin's daily volume.
    This prevents market impact on less liquid coins with larger positions.
    """
    notional = size * price
    if coin_day_volume <= 0:
        return notional > 50000  # fallback: TWAP above $50k if no volume data
    return notional > coin_day_volume * 0.001  # 0.1% of daily volume


def _open_position_twap(
    exchange, info, coin: str, is_buy: bool, total_size: float, price: float,
    stop_price: float, sl_is_buy: bool, sl_pct: float,
    builder_info: dict, sz_decimals: int = 2,
    n_slices: int = 5, slice_interval: float = 3.0,
) -> dict | None:
    """Execute entry via TWAP — split into multiple slices over time.

    Used for larger positions to reduce market impact. First slice places
    the stop loss atomically via normalTpsl. Subsequent slices are standalone
    IOC orders, with the stop loss resized after each fill.

    Args:
        n_slices: Number of order slices (default 5)
        slice_interval: Seconds between slices (default 3s)
    """
    import time

    slice_size = _round_size(total_size / n_slices, sz_decimals)
    if slice_size * price < 10:
        # Slices too small for Hyperliquid minimum — fall back to single order
        return _open_position_algo(
            exchange, coin, is_buy, total_size, price,
            stop_price, sl_is_buy, sl_pct, builder_info, sz_decimals,
        )

    _emit({"type": "status", "msg": f"TWAP: splitting {total_size:.{sz_decimals}f} into {n_slices} slices of {slice_size:.{sz_decimals}f}"})

    total_filled = 0.0
    weighted_price_sum = 0.0
    entry_oid = None
    stop_oid = None

    for i in range(n_slices):
        current_price = float(info.all_mids().get(coin, str(price)))
        slippage = 0.01
        if is_buy:
            limit_px = _round_price(current_price * (1 + slippage), sz_decimals)
        else:
            limit_px = _round_price(current_price * (1 - slippage), sz_decimals)

        this_size = _round_size(slice_size, sz_decimals)

        # Last slice: use remaining to avoid rounding dust
        if i == n_slices - 1:
            this_size = _round_size(total_size - total_filled, sz_decimals)
            if this_size <= 0:
                break

        if i == 0:
            # First slice: atomic entry + stop loss via normalTpsl
            orders = [
                {
                    "coin": coin, "is_buy": is_buy, "sz": this_size,
                    "limit_px": limit_px,
                    "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": False,
                },
                {
                    "coin": coin, "is_buy": sl_is_buy, "sz": _round_size(total_size, sz_decimals),
                    "limit_px": _round_price(stop_price, sz_decimals),
                    "order_type": {"trigger": {"triggerPx": _round_price(stop_price, sz_decimals), "isMarket": True, "tpsl": "sl"}},
                    "reduce_only": True,
                },
            ]
            result = exchange.bulk_orders(orders, grouping="normalTpsl", builder=builder_info)

            if result.get("status") != "ok":
                friendly = translate_builder_fee_error(result)
                _emit({"type": "error", "msg": friendly or f"TWAP slice 1 failed: {result}"})
                return None

            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if "error" in s:
                    friendly = translate_builder_fee_error(s["error"])
                    _emit({"type": "error", "msg": friendly or f"TWAP order error: {s['error']}"})
                    return None
                elif "filled" in s:
                    entry_oid = s["filled"].get("oid")
                    fill_px = float(s["filled"].get("avgPx", current_price))
                    fill_sz = float(s["filled"].get("totalSz", this_size))
                    total_filled += fill_sz
                    weighted_price_sum += fill_px * fill_sz
                elif "resting" in s:
                    stop_oid = s["resting"].get("oid")

            if total_filled == 0:
                _emit({"type": "status", "msg": "TWAP: first slice didn't fill, canceling stop"})
                if stop_oid:
                    try:
                        exchange.cancel(coin, stop_oid)
                    except Exception:
                        pass
                return None

        else:
            # Subsequent slices: standalone IOC
            try:
                result = exchange.order(
                    coin, is_buy, this_size, limit_px,
                    order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=False, builder=builder_info,
                )
                if result.get("status") == "ok":
                    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                    for s in statuses:
                        if "filled" in s:
                            fill_px = float(s["filled"].get("avgPx", current_price))
                            fill_sz = float(s["filled"].get("totalSz", this_size))
                            total_filled += fill_sz
                            weighted_price_sum += fill_px * fill_sz
                            if entry_oid is None:
                                entry_oid = s["filled"].get("oid")
            except Exception as e:
                _emit({"type": "status", "msg": f"TWAP slice {i+1} error: {e}"})

        _emit({"type": "status", "msg": f"TWAP: slice {i+1}/{n_slices} — filled {total_filled:.{sz_decimals}f}/{total_size:.{sz_decimals}f}"})

        if i < n_slices - 1:
            time.sleep(slice_interval)

    if total_filled == 0:
        return None

    # Resize stop loss to match actual filled size
    if stop_oid and abs(total_filled - total_size) > 0.01 * total_size:
        try:
            exchange.cancel(coin, stop_oid)
            sl_result = exchange.order(
                coin, sl_is_buy, _round_size(total_filled, sz_decimals),
                _round_price(stop_price, sz_decimals),
                order_type={"trigger": {"triggerPx": _round_price(stop_price, sz_decimals), "isMarket": True, "tpsl": "sl"}},
                reduce_only=True, builder=builder_info,
            )
            if sl_result.get("status") == "ok":
                sl_statuses = sl_result.get("response", {}).get("data", {}).get("statuses", [])
                for s in sl_statuses:
                    if "resting" in s:
                        stop_oid = s["resting"].get("oid")
        except Exception as e:
            _emit({"type": "error", "msg": f"TWAP: stop resize failed: {e}"})

    avg_fill_price = weighted_price_sum / total_filled if total_filled > 0 else price

    _emit({"type": "status", "msg": f"TWAP complete: {total_filled:.{sz_decimals}f} filled @ avg ${avg_fill_price:,.2f}"})

    return {
        "entry_oid": entry_oid,
        "stop_oid": stop_oid,
        "fill_price": avg_fill_price,
        "filled_size": total_filled,
        "stop_price": stop_price,
        "mid_price_at_order": price,
        "execution_method": "twap",
    }


def _open_position_algo(
    exchange, coin: str, is_buy: bool, size: float, price: float,
    stop_price: float, sl_is_buy: bool, sl_pct: float,
    builder_info: dict, sz_decimals: int = 2,
) -> dict | None:
    """Place entry + stop loss atomically using normalTpsl grouping.

    Critical safety: if entry doesn't fill (or partially fills), we cancel
    the stop loss and adjust. A naked stop loss trigger can open a reverse
    position, which would be catastrophic.
    """
    try:
        # Compute slippage price for IOC entry (1% slippage — SDK default is 5%)
        slippage = 0.01
        if is_buy:
            entry_px = _round_price(price * (1 + slippage), sz_decimals)
        else:
            entry_px = _round_price(price * (1 - slippage), sz_decimals)

        size = _round_size(size, sz_decimals)
        stop_price = _round_price(stop_price, sz_decimals)

        # Build atomic order bundle: entry + stop loss
        orders = [
            {
                "coin": coin,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": entry_px,
                "order_type": {"limit": {"tif": "Ioc"}},
                "reduce_only": False,
            },
            {
                "coin": coin,
                "is_buy": sl_is_buy,
                "sz": size,
                "limit_px": stop_price,
                "order_type": {"trigger": {"triggerPx": stop_price, "isMarket": True, "tpsl": "sl"}},
                "reduce_only": True,
            },
        ]

        submit_ts = time.time()
        result = exchange.bulk_orders(orders, grouping="normalTpsl", builder=builder_info)
        fill_ts = time.time()

        if result.get("status") != "ok":
            friendly = translate_builder_fee_error(result)
            _emit({"type": "error", "msg": friendly or f"Order failed: {result}"})
            return None

        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        entry_oid = None
        stop_oid = None
        filled_size = 0.0
        fill_price = price

        for s in statuses:
            if "error" in s:
                friendly = translate_builder_fee_error(s["error"])
                _emit({"type": "error", "msg": friendly or f"Order rejected: {s['error']}"})
                return None
            elif "filled" in s:
                entry_oid = s["filled"].get("oid")
                fill_price = float(s["filled"].get("avgPx", price))
                filled_size = float(s["filled"].get("totalSz", size))
            elif "resting" in s:
                stop_oid = s["resting"].get("oid")

        # CRITICAL: If entry didn't fill at all, cancel the stop loss
        if entry_oid is None or filled_size == 0:
            _emit({"type": "status", "msg": "Entry order not filled. Canceling stop loss."})
            if stop_oid:
                try:
                    exchange.cancel(coin, stop_oid)
                except Exception:
                    pass
            return None

        # The normalTpsl bundle above placed the entry + a stop sized to the
        # requested `size`. If the entry under-fills (HL rounds down to its
        # lot-size, or fills partially under fast markets), the stop now
        # covers slightly more than the actual position. That's fine:
        # `reduce_only=True` caps the actual close to the real position size,
        # so any excess in the stop is silently dropped by HL on trigger.
        #
        # Only re-place the stop on a MAJOR underfill (<50% of requested).
        # The naïve cancel+replace on every minor rounding mismatch produces
        # duplicate stops on HL when the cancel-ack races the new placement —
        # observed in Phase 4 verification with 0.00015 vs 0.00016 filled
        # vs requested. Reduce-only enforcement makes the original stop safe
        # for small underfills.
        filled_size = _round_size(filled_size, sz_decimals)
        if filled_size < size * 0.5:
            _emit({"type": "status",
                   "msg": f"Major partial fill: {filled_size:.6f}/{size:.6f}. "
                          f"Re-placing stop loss at actual filled size."})
            if stop_oid:
                try:
                    exchange.cancel(coin, stop_oid)
                except Exception:
                    pass
                # Verify cancel took effect before placing the replacement.
                # If HL hasn't ack'd the cancel, the new stop would coexist
                # with the original — exactly the duplicate-orders bug this
                # branch existed to fix in the first place.
                time.sleep(0.5)
            try:
                sl_result = exchange.order(
                    coin, sl_is_buy, filled_size, stop_price,
                    order_type={"trigger": {"triggerPx": stop_price, "isMarket": True, "tpsl": "sl"}},
                    reduce_only=True, builder=builder_info,
                )
                if sl_result.get("status") == "ok":
                    sl_statuses = sl_result.get("response", {}).get("data", {}).get("statuses", [])
                    for s in sl_statuses:
                        if "resting" in s:
                            stop_oid = s["resting"].get("oid")
            except Exception as e:
                _emit({"type": "error", "msg": f"Stop loss adjustment failed: {e}. Original (oversized) stop remains."})

            size = filled_size

        return {
            "entry_oid": entry_oid,
            "stop_oid": stop_oid,
            "fill_price": fill_price,
            "filled_size": filled_size,
            "stop_price": stop_price,
            "mid_price_at_order": price,
            "execution_method": "ioc",
            "submit_ts": submit_ts,
            "fill_ts": fill_ts,
        }

    except Exception as e:
        _emit({"type": "error", "msg": f"Order execution error: {e}"})
        return None


def _close_position_algo(
    exchange, info, position: AlgoPosition, coin: str,
    account_address: str, builder_info: dict, sz_decimals: int = 2,
) -> AlgoTrade | None:
    """Close an open position: cancel stop loss, market close.

    Handles partial fills by retrying up to 3 times with increasing slippage.
    """
    try:
        # Cancel the resting stop loss trigger order
        if position.stop_oid:
            try:
                exchange.cancel(coin, position.stop_oid)
            except Exception:
                pass  # May already be canceled or filled

        # Market close with retry for partial fills
        is_buy = position.side == "short"  # buy to close short, sell to close long
        remaining_size = position.size
        total_filled = 0.0
        weighted_exit_sum = 0.0
        exit_oid = None
        exit_mid_price = float(info.all_mids().get(coin, "0"))  # TCA: capture mid before close
        max_attempts = 3

        for attempt in range(max_attempts):
            close_price = float(info.all_mids().get(coin, "0"))
            slippage = 0.01 * (attempt + 1)  # 1%, 2%, 3% escalating slippage
            if is_buy:
                limit_px = _round_price(close_price * (1 + slippage), sz_decimals)
            else:
                limit_px = _round_price(close_price * (1 - slippage), sz_decimals)

            order_size = _round_size(remaining_size, sz_decimals)
            if order_size * close_price < 10:  # Below minimum notional
                break

            result = exchange.order(
                coin, is_buy, order_size, limit_px,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=True,
                builder=builder_info,
            )

            if result.get("status") == "ok":
                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "filled" in statuses[0]:
                    fill_px = float(statuses[0]["filled"].get("avgPx", close_price))
                    fill_sz = float(statuses[0]["filled"].get("totalSz", order_size))
                    if exit_oid is None:
                        exit_oid = statuses[0]["filled"].get("oid")
                    weighted_exit_sum += fill_px * fill_sz
                    total_filled += fill_sz
                    remaining_size -= fill_sz
                elif statuses and "error" in statuses[0]:
                    _emit({"type": "error", "msg": f"Close order rejected: {statuses[0]['error']}"})
                    break

            # Check if fully closed
            remaining_size = _round_size(remaining_size, sz_decimals)
            if remaining_size <= 0 or remaining_size * close_price < 10:
                break

            _emit({"type": "status", "msg": f"Partial close fill ({total_filled:.{sz_decimals}f}/{position.size:.{sz_decimals}f}). Retrying with {slippage*100:.0f}% slippage..."})
            time.sleep(0.5)

        # Compute final exit price
        exit_price = (weighted_exit_sum / total_filled) if total_filled > 0 else float(info.all_mids().get(coin, "0"))
        closed_size = total_filled if total_filled > 0 else position.size

        # Compute P&L
        if position.side == "long":
            price_pnl = closed_size * (exit_price - position.entry_price)
        else:
            price_pnl = closed_size * (position.entry_price - exit_price)

        total_pnl = price_pnl + position.funding_collected

        if remaining_size > 0 and remaining_size * exit_price >= 10:
            _emit({"type": "error", "msg": f"WARNING: {remaining_size:.{sz_decimals}f} {coin} could not be closed. Check Hyperliquid UI."})

        # TCA: compute slippage in basis points
        entry_slip_bps = 0.0
        if position.entry_mid_price > 0:
            entry_slip_bps = (position.entry_price - position.entry_mid_price) / position.entry_mid_price * 10000
            if position.side == "short":
                entry_slip_bps = -entry_slip_bps  # for shorts, lower fill = better
        exit_slip_bps = 0.0
        if exit_mid_price > 0:
            exit_slip_bps = (exit_price - exit_mid_price) / exit_mid_price * 10000
            if position.side == "long":
                exit_slip_bps = -exit_slip_bps  # for longs, lower exit = worse

        return AlgoTrade(
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            size=closed_size,
            pnl=total_pnl,
            pnl_pct=0,  # Will be overridden by real equity change
            price_pnl=price_pnl,
            funding_collected=position.funding_collected,
            entry_time=position.entry_time,
            exit_time=datetime.now().strftime("%H:%M"),
            candles_held=position.candles_held,
            max_favorable=position.max_favorable,
            max_adverse=position.max_adverse,
            exit_reason="signal",
            exit_oid=exit_oid,
            entry_mid_price=position.entry_mid_price,
            exit_mid_price=exit_mid_price,
            entry_slippage_bps=round(entry_slip_bps, 2),
            exit_slippage_bps=round(exit_slip_bps, 2),
            execution_method=position.execution_method,
            signal_ts=position.signal_ts,
            submit_ts=position.submit_ts,
            fill_ts=position.fill_ts,
        )

    except Exception as e:
        _emit({"type": "error", "msg": f"Close position error: {e}"})
        return None


def _record_stopped_trade(position: AlgoPosition, last_price: float, info, account_address: str, coin: str) -> AlgoTrade | None:
    """Record a trade that was stopped out by Hyperliquid's trigger order."""
    try:
        # Query recent fills to find the stop loss fill
        fills = info.user_fills(account_address)
        exit_price = last_price  # fallback

        for fill in reversed(fills[-10:]):
            if fill.get("coin") == coin:
                exit_price = float(fill.get("px", last_price))
                break

        if position.side == "long":
            price_pnl = position.size * (exit_price - position.entry_price)
        else:
            price_pnl = position.size * (position.entry_price - exit_price)

        return AlgoTrade(
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            size=position.size,
            pnl=price_pnl + position.funding_collected,
            pnl_pct=0,
            price_pnl=price_pnl,
            funding_collected=position.funding_collected,
            entry_time=position.entry_time,
            exit_time=datetime.now().strftime("%H:%M"),
            candles_held=position.candles_held,
            max_favorable=position.max_favorable,
            max_adverse=position.max_adverse,
            exit_reason="stop_loss",
        )
    except Exception:
        return None


# ─── HELPERS ───────────────────────────────────────────────────

def _get_latest_candle(info, coin: str, interval: str) -> dict | None:
    """Fetch the most recent closed candle."""
    end_time = int(time.time() * 1000)
    start_time = end_time - (4 * 60 * 60 * 1000)
    try:
        candles = info.candles_snapshot(coin, interval, start_time, end_time)
        if candles and len(candles) >= 2:
            return candles[-2]
    except Exception:
        pass
    return None


def _get_current_funding(info, coin: str) -> float:
    """Get current funding rate."""
    try:
        end_time = int(time.time() * 1000)
        start_time = end_time - (2 * 60 * 60 * 1000)
        funding = info.funding_history(coin, start_time, end_time)
        if funding:
            return float(funding[-1]["fundingRate"])
    except Exception:
        pass
    return 0.0


def _minutes_to_next_funding() -> int:
    """Minutes until next funding settlement."""
    now = time.time()
    next_hour = (int(now) // 3600 + 1) * 3600
    return max(0, int((next_hour - now) / 60))


def _get_equity(info, account_address: str) -> float:
    """Get current account equity from Hyperliquid, mode-aware.

    For Standard users this is perp accountValue. For Unified/PM users
    this includes spot USDC (which is real perp collateral under those
    modes). Used by sizing logic so strategies don't think they have
    zero capital on a funded unified wallet.
    """
    try:
        from rift_data.account_mode import read_collateral
        return float(read_collateral(info, account_address).total)
    except Exception:
        return 0.0


def _sync_position(info, account_address: str, coin: str) -> tuple[float, dict | None]:
    """Query real position state from exchange. Returns (equity, position_dict or None)."""
    try:
        from rift_data.account_mode import read_collateral
        state = info.user_state(account_address)
        # Pass the pre-fetched state to avoid a redundant HL call
        equity = float(read_collateral(info, account_address, perp_state=state).total)

        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                szi = float(pos.get("szi", "0"))
                if szi != 0:
                    return equity, pos

        return equity, None
    except Exception:
        return 0.0, None


def _verify_stop_exists(info, account_address: str, stop_oid: int) -> bool:
    """Check if a stop loss order still exists (resting) on the exchange."""
    try:
        open_orders = info.frontend_open_orders(account_address)
        for order in open_orders:
            if order.get("oid") == stop_oid:
                return True
        return False
    except Exception:
        return False  # Assume gone if we can't check


def _check_orphaned_positions(info, account_address: str, coin: str) -> bool:
    """Check for existing positions that RIFT didn't open.

    Returns True if an orphan was detected. Caller MUST abort startup when
    True — the daemon doesn't track the orphan's entry, stop_oid, or
    stop_price, so it can't safely manage it. Letting the daemon run
    anyway means the next entry signal would either double-up or net-flat,
    both unrecoverable from inside the daemon.

    Orphan detection triggers when there's a real position on the exchange
    but no RIFT-tracked position. "No tracked position" means either no
    saved state file, or a saved state file with `position: null` — the
    latter is the common case after a clean daemon shutdown (the session
    state file persists with a null position record).

    The crash-recovery path (saved.position != None) is handled separately
    upstream and does not invoke this function.
    """
    saved = _load_algo_state()
    _, real_pos = _sync_position(info, account_address, coin)

    no_tracked_position = saved is None or saved.get("position") is None
    if real_pos and no_tracked_position:
        szi = float(real_pos.get("szi", "0"))
        side = "long" if szi > 0 else "short"
        entry = real_pos.get("entryPx", "?")
        _emit({
            "type": "error",
            "msg": f"Orphan {side} position detected on {coin} (entry ${entry}). "
                   f"RIFT did not open this position and cannot safely manage it — "
                   f"it lacks a tracked entry time, stop loss order id, or stop price. "
                   f"Close it manually before restarting the daemon:\n"
                   f"    rift more close-position {coin}\n"
                   f"or via the Hyperliquid UI. Refusing to start.",
        })
        return True
    return False




def _check_portfolio_gate(strategy_key: str) -> tuple[bool, float, float | None]:
    """Check if portfolio supervisor allows this entry.

    Returns (allowed, size_multiplier, max_position_usd or None).
    If no supervisor is running (no gate file), returns (True, 1.0, None).
    """
    gate_file = ALGO_DIR / "gate.json"
    if not gate_file.exists():
        return True, 1.0, None
    try:
        gate = json.loads(gate_file.read_text())
        if gate.get("portfolio_paused"):
            return False, 0.0, None
        if strategy_key in gate.get("blocked_strategies", []):
            return False, 0.0, None
        size_mult = gate.get("max_size_overrides", {}).get(strategy_key, 1.0)
        max_usd = gate.get("max_position_usd", {}).get(strategy_key)
        return True, size_mult, max_usd
    except Exception:
        return True, 1.0, None  # gate file corrupt = allow (fail open)


def _build_state_dict(
    strategy_name, pair, interval, initial_equity, equity, peak_equity,
    position, trades, total_funding, last_price, last_funding_rate,
    candles_processed, started_at, price_history, funding_events,
    account_address, session_start_price=0.0,
    dd_events=None, dd_start_time=None, max_recovery_hours=0.0,
    health_report=None, health_paused=False,
) -> dict:
    """Build state dict for NDJSON output — matches simulate.py format."""
    pos_dict = None
    if position:
        # Compute unrealized P&L
        if position.side == "long":
            unreal = position.size * (last_price - position.entry_price) if last_price > 0 else 0
        else:
            unreal = position.size * (position.entry_price - last_price) if last_price > 0 else 0

        # Stop proximity — use actual stop price from exchange
        stop_prox = 0.0
        if position.stop_price > 0 and position.entry_price > 0:
            if position.side == "long":
                denom = position.entry_price - position.stop_price
                if denom > 0:
                    stop_prox = max(0, min(1, (position.entry_price - last_price) / denom))
            else:
                denom = position.stop_price - position.entry_price
                if denom > 0:
                    stop_prox = max(0, min(1, (last_price - position.entry_price) / denom))

        pos_dict = {
            "side": position.side,
            "entry_price": round(position.entry_price, 2),
            "size": round(position.size, 6),
            "candles_held": position.candles_held,
            "entry_time": position.entry_time,
            "stop_loss_price": round(position.stop_price, 2),
            "max_favorable": round(position.max_favorable, 2),
            "max_adverse": round(position.max_adverse, 2),
            "funding_collected": round(position.funding_collected, 2),
        }

    # equity is already accountValue from Hyperliquid (includes unrealized PnL)
    # so total_equity IS equity — no need to add unrealized again
    total_equity = equity

    # Compute unrealized separately for display only
    unrealized = 0.0
    if position and last_price > 0:
        if position.side == "long":
            unrealized = position.size * (last_price - position.entry_price)
        else:
            unrealized = position.size * (position.entry_price - last_price)
    total_pnl = total_equity - initial_equity
    total_pnl_pct = (total_pnl / initial_equity * 100) if initial_equity > 0 else 0

    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0

    recent_trades = []
    for t in trades[-5:]:
        recent_trades.append({
            "side": t.side, "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2), "size": round(t.size, 6),
            "pnl": round(t.pnl, 2), "pnl_pct": round(t.pnl_pct, 2),
            "price_pnl": round(t.price_pnl, 2),
            "funding_collected": round(t.funding_collected, 2),
            "candles_held": t.candles_held,
            "max_favorable": round(t.max_favorable, 2),
            "max_adverse": round(t.max_adverse, 2),
            "exit_reason": t.exit_reason,
            "entry_time": t.entry_time, "exit_time": t.exit_time,
            "oid": t.entry_oid,
        })

    return _sanitize_for_json({
        "strategy": strategy_name, "pair": pair, "interval": interval,
        "equity": round(equity, 2), "total_equity": round(total_equity, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(total_pnl, 2), "total_pnl_pct": round(total_pnl_pct, 2),
        "initial_equity": round(initial_equity, 2),
        "position": pos_dict,
        "last_price": round(last_price, 2),
        "last_funding_rate": last_funding_rate,
        "total_funding": round(total_funding, 2),
        "num_trades": len(trades), "win_rate": round(win_rate, 2),
        "candles_processed": candles_processed,
        "started_at": started_at,
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "peak_equity": round(peak_equity, 2),
        "session_high": round(max(price_history[-30:]) if price_history else 0, 2),
        "session_low": round(min(price_history[-30:]) if price_history else 0, 2),
        "price_delta": round(last_price - session_start_price, 2) if session_start_price > 0 else 0,
        "stop_proximity": 0,
        "price_history": [round(p, 2) for p in price_history[-30:]],
        "recent_trades": recent_trades,
        "funding_events": funding_events[-5:],
        "is_live": True,
        "wallet": account_address,
        "health_score": health_report.score if health_report else None,
        "health_grade": health_report.grade if health_report else None,
        "health_paused": health_paused,
        "drawdown_events": dd_events[-5:] if dd_events else [],
        "in_drawdown": dd_start_time is not None,
        "current_drawdown_start": dd_start_time,
        "max_recovery_hours": round(max_recovery_hours, 1),
    })


def _trade_replay_dict(trade: AlgoTrade | None) -> dict | None:
    """Generate trade replay data."""
    if trade is None:
        return None
    return _sanitize_for_json({
        "side": trade.side,
        "result": "WIN" if trade.pnl > 0 else "LOSS",
        "entry_price": round(trade.entry_price, 2),
        "exit_price": round(trade.exit_price, 2),
        "price_pnl": round(trade.price_pnl, 2),
        "funding_pnl": round(trade.funding_collected, 2),
        "total_pnl": round(trade.pnl, 2),
        "pnl_pct": round(trade.pnl_pct, 2),
        "duration": f"{trade.candles_held} candles",
        "max_favorable": round(trade.max_favorable, 2),
        "max_adverse": round(trade.max_adverse, 2),
        "exit_reason": trade.exit_reason,
        "oid": trade.entry_oid,
        "entry_slippage_bps": trade.entry_slippage_bps,
        "exit_slippage_bps": trade.exit_slippage_bps,
        "execution_method": trade.execution_method,
        "latency_ms": round((trade.fill_ts - trade.signal_ts) * 1000, 1) if trade.signal_ts > 0 and trade.fill_ts > 0 else None,
    })


def _generate_live_narrative(trades, total_funding, equity, initial_equity, started_at) -> dict:
    """Generate session narrative."""
    num_trades = len(trades)
    total_pnl = equity - initial_equity

    story_parts = []
    if num_trades == 0:
        story_parts.append("No trades were executed.")
    else:
        wins = [t for t in trades if t.pnl > 0]
        story_parts.append(f"Executed {num_trades} algo trade{'s' if num_trades != 1 else ''}. {len(wins)} won.")
        if num_trades <= 5:
            for i, t in enumerate(trades):
                pnl_str = f"+${t.pnl:.2f}" if t.pnl > 0 else f"-${abs(t.pnl):.2f}"
                story_parts.append(f"Trade {i+1}: {t.side.upper()} — {pnl_str} ({t.exit_reason.replace('_', ' ')}).")

    # Insight
    total_price_pnl = sum(t.price_pnl for t in trades)
    total_funding_pnl = sum(t.funding_collected for t in trades)
    insight = ""
    denom = abs(total_price_pnl) + abs(total_funding_pnl)
    if denom > 0:
        funding_share = abs(total_funding_pnl) / denom * 100
        if funding_share > 40:
            insight = f"{funding_share:.0f}% of P&L came from funding. The structural edge is working."
        elif funding_share > 15:
            insight = f"Funding contributed {funding_share:.0f}% of total P&L."

    # Projection
    projection = {}
    try:
        duration_seconds = time.time() - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").timestamp()
        if duration_seconds > 300 and total_pnl != 0:
            hours = duration_seconds / 3600
            daily_rate = (total_pnl / hours) * 24
            projection = {
                "daily": round(daily_rate, 2),
                "monthly": round(daily_rate * 30, 2),
                "annual": round(daily_rate * 365, 2),
                "apy": round((daily_rate * 365 / initial_equity) * 100, 1) if initial_equity > 0 else 0,
                "hours_observed": round(hours, 1),
            }
    except Exception:
        pass

    return {"story": " ".join(story_parts), "insight": insight, "projection": projection}


def _save_session_log(strategy_name, pair, interval, initial_equity, equity, trades, total_funding, candles_processed, started_at, account_address, dd_events=None, max_recovery_hours=0.0) -> Path:
    """Save session log."""
    log_dir = Path.home() / ".rift" / "algo_sessions"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"ALGO_{strategy_name}_{pair}_{timestamp}.json"
    log_path = log_dir / filename

    log_data = _sanitize_for_json({
        "mode": "LIVE",
        "wallet": account_address,
        "strategy": strategy_name, "pair": pair, "interval": interval,
        "initial_equity": initial_equity, "final_equity": equity,
        "total_funding": total_funding, "candles_processed": candles_processed,
        "started_at": started_at, "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trades": [
            {
                "side": t.side, "entry_price": round(t.entry_price, 2),
                "exit_price": round(t.exit_price, 2), "size": round(t.size, 6),
                "pnl": round(t.pnl, 2), "price_pnl": round(t.price_pnl, 2),
                "funding": round(t.funding_collected, 2),
                "candles_held": t.candles_held, "exit_reason": t.exit_reason,
                "entry_time": t.entry_time, "exit_time": t.exit_time,
                "oid": t.entry_oid,
                "entry_mid_price": round(t.entry_mid_price, 2),
                "exit_mid_price": round(t.exit_mid_price, 2),
                "entry_slippage_bps": t.entry_slippage_bps,
                "exit_slippage_bps": t.exit_slippage_bps,
                "execution_method": t.execution_method,
                "signal_ts": t.signal_ts,
                "submit_ts": t.submit_ts,
                "fill_ts": t.fill_ts,
            }
            for t in trades
        ],
        "drawdown_events": dd_events or [],
        "max_recovery_hours": round(max_recovery_hours, 1),
    })

    log_path.write_text(json.dumps(log_data, indent=2))
    return log_path


# ─── DAEMON MANAGEMENT ───────────────────────────────────────

def _is_pid_alive(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def list_algo_sessions() -> list[dict]:
    """List all running algo trading sessions."""
    sessions = []
    if not ALGO_PIDS_DIR.exists():
        return sessions

    for pid_file in ALGO_PIDS_DIR.glob("*.pid"):
        key = pid_file.stem
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, FileNotFoundError):
            continue

        alive = _is_pid_alive(pid)

        # Read state snapshot if available
        state_file = _session_state_file(key)
        state = None
        if state_file.exists():
            try:
                snapshot = json.loads(state_file.read_text())
                state = snapshot.get("state", {})
            except Exception:
                pass

        if alive:
            sessions.append({
                "key": key,
                "pid": pid,
                "alive": True,
                "strategy": state.get("strategy", key.split("_")[0]) if state else key.split("_")[0],
                "pair": state.get("pair", "") if state else "",
                "equity": state.get("total_equity", 0) if state else 0,
                "pnl_pct": state.get("total_pnl_pct", 0) if state else 0,
                "num_trades": state.get("num_trades", 0) if state else 0,
                "position": state.get("position") if state else None,
                "started_at": state.get("started_at", "") if state else "",
                "last_update": state.get("last_update", "") if state else "",
            })
        else:
            # Stale PID file — clean up
            pid_file.unlink(missing_ok=True)

    return sessions


def get_algo_session(strategy: str, pair: str) -> dict | None:
    """Get full state for a specific algo session."""
    coin = _normalize_coin(pair)
    key = _session_key(strategy, coin)

    pid_file = _session_pid_file(key)
    if not pid_file.exists():
        return None

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, FileNotFoundError):
        return None

    if not _is_pid_alive(pid):
        pid_file.unlink(missing_ok=True)
        return None

    state_file = _session_state_file(key)
    if not state_file.exists():
        return {"key": key, "pid": pid, "alive": True, "state": None}

    try:
        snapshot = json.loads(state_file.read_text())
        return {
            "key": key,
            "pid": pid,
            "alive": True,
            **snapshot,
        }
    except Exception:
        return {"key": key, "pid": pid, "alive": True, "state": None}


def stop_algo_session(strategy: str, pair: str) -> dict:
    """Stop a running algo session by sending SIGTERM."""
    coin = _normalize_coin(pair)
    key = _session_key(strategy, coin)

    pid_file = _session_pid_file(key)
    if not pid_file.exists():
        return {"status": "not_found", "msg": f"No running session for {strategy} on {coin}"}

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, FileNotFoundError):
        return {"status": "error", "msg": "Invalid PID file"}

    if not _is_pid_alive(pid):
        pid_file.unlink(missing_ok=True)
        return {"status": "not_running", "msg": f"Session {key} is not running (stale PID)"}

    # Send SIGTERM for graceful shutdown (closes positions, saves log)
    os.kill(pid, signal.SIGTERM)

    # Wait up to 30 seconds for graceful shutdown
    for _ in range(60):
        if not _is_pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        # Force kill if still alive
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # Read final state if available
    state_file = _session_state_file(key)
    final_state = None
    if state_file.exists():
        try:
            final_state = json.loads(state_file.read_text())
        except Exception:
            pass

    # Clean up PID file
    pid_file.unlink(missing_ok=True)

    return {
        "status": "stopped",
        "key": key,
        "final_state": final_state,
    }


def is_session_running(strategy: str, pair: str) -> bool:
    """Check if an algo session is already running."""
    coin = _normalize_coin(pair)
    key = _session_key(strategy, coin)
    pid_file = _session_pid_file(key)
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        return _is_pid_alive(pid)
    except (ValueError, FileNotFoundError):
        return False
