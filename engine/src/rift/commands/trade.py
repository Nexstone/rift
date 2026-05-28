"""Live trading / manual orders / sessions commands — extracted from cli.py in Phase 6.

The user-facing command surface is unchanged. Each command is registered
on the shared Typer `app` in `rift.commands._shared`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import typer

from rift.commands._shared import app, _emit, _hint, _sanitize_for_json
from rift_core.config import parse_duration as _parse_duration


def _hl_order_error(resp: dict) -> str | None:
    """Return the first order-level error message from an HL exchange
    response, or None if the order was accepted.

    HL's exchange.market_open() returns an outer `{status: "ok"}` even
    when the inner order is rejected (insufficient margin, below min,
    invalid size, etc.). The real reject status lives at
    `resp.response.data.statuses[i].error`. Without unwrapping this,
    callers display a success result for a rejected order.
    """
    try:
        statuses = resp.get("response", {}).get("data", {}).get("statuses", []) or []
        for s in statuses:
            if isinstance(s, dict) and s.get("error"):
                return s["error"]
    except (AttributeError, TypeError):
        pass
    return None


def _resolve_spot_pair(info, user_coin: str) -> tuple[str, str, float, int]:
    """Resolve a user-facing coin name to HL spot details.

    Returns (pair_id, hl_token_name, price, sz_decimals).

    HL spot uses @N pair identifiers (e.g. @142 for UBTC/USDC) in
    info.all_mids() and exchange.market_open(), NOT literal pair names
    like "BTC/USDC". Some tokens use U-prefixed names on spot
    (BTC → UBTC, ETH → UETH) while others keep their literal name
    (HYPE, PURR). The spot_user_state holdings dict is keyed by the
    HL token name (UBTC, not BTC). Each token also has its own
    szDecimals precision — HL's `float_to_wire` will reject any size
    with more decimal places than that, so the caller must round.

    Raises ValueError when no pair is found.
    """
    name = (
        user_coin.strip().upper()
        .replace("/USDC", "")
        .replace("-PERP", "")
        .replace("-SPOT", "")
    )
    meta = info.spot_meta()
    tokens_by_name = {t["name"]: t for t in meta["tokens"]}
    # Try literal name first, then U-prefix variant (BTC → UBTC, ETH → UETH).
    candidates = [name]
    if not name.startswith("U"):
        candidates.append(f"U{name}")
    for candidate in candidates:
        if candidate not in tokens_by_name:
            continue
        tok = tokens_by_name[candidate]
        token_idx = tok["index"]
        sz_decimals = int(tok.get("szDecimals", 4))
        for p in meta["universe"]:
            if (
                len(p["tokens"]) >= 2
                and p["tokens"][0] == token_idx
                and p["tokens"][1] == 0  # 0 = USDC
            ):
                pair_id = p["name"]
                mids = info.all_mids()
                price = float(mids.get(pair_id, 0))
                return pair_id, candidate, price, sz_decimals
    raise ValueError(
        f"No Hyperliquid spot pair found for '{user_coin}'. "
        f"Try the HL spot token name directly (e.g. UBTC, UETH, HYPE, PURR). "
        f"List available pairs with `rift more list-pairs`."
    )


@app.command("buy")
def buy(
    coin: str = typer.Argument(..., help="Token to buy (e.g. HYPE, ETH, BTC)"),
    amount: float = typer.Option(0, "--amount", help="USDC amount to spend"),
    size: float = typer.Option(0, "--size", help="Token amount to buy"),
) -> None:
    """Buy a token on the spot market."""
    from rift.trading_gates import require_trading_ready
    from rift.builder_fee import get_builder_info

    result = require_trading_ready()
    if result is None:
        return
    private_key, account_address = result

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account

    base_url = constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    wallet = Account.from_key(private_key)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    # Resolve user-facing name → HL @N pair + token name + current mid +
    # per-token szDecimals (size precision). HL spot mids and
    # exchange.market_open() use @N identifiers, not literal "<TOKEN>/USDC"
    # names. Sizes must be rounded to szDecimals or HL's float_to_wire
    # rejects the order.
    try:
        pair, token, price, sz_decimals = _resolve_spot_pair(info, coin)
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        return
    if price <= 0:
        _emit({"type": "error", "msg": f"No live price for {token} spot pair {pair}."})
        return

    # Calculate size + round to HL's szDecimals
    if amount > 0:
        buy_size = amount / price
    elif size > 0:
        buy_size = size
    else:
        _emit({"type": "error", "msg": "Specify --amount (USDC to spend) or --size (tokens to buy)"})
        return
    buy_size = round(buy_size, sz_decimals)
    if buy_size <= 0:
        _emit({"type": "error",
               "msg": f"Computed size {buy_size} {token} rounds to zero at "
                      f"szDecimals={sz_decimals}. Increase --amount or use --size with a value above "
                      f"{10 ** -sz_decimals} {token}."})
        return

    builder_info = get_builder_info("spot")
    _emit({"type": "progress", "pct": 50, "msg": f"Buying {buy_size} {token} at ~${price:.4f}..."})

    try:
        resp = exchange.market_open(pair, is_buy=True, sz=buy_size, builder=builder_info)
        # HL nests order-level errors inside an outer status:'ok' shape. The
        # outer "ok" only means the request was syntactically valid; the
        # actual fill/reject is in response.data.statuses[].
        order_err = _hl_order_error(resp)
        if order_err:
            _emit({"type": "error", "msg": f"Buy rejected by Hyperliquid: {order_err}"})
            return
        _emit({
            "type": "result", "command": "buy", "market": "spot",
            "token": token, "pair": pair, "size": round(buy_size, 8),
            "price": price, "total_cost": round(buy_size * price, 2),
            "response": resp,
        })
        _hint(f"Check holdings with 'rift more holdings'")
    except Exception as e:
        _emit({"type": "error", "msg": f"Buy failed: {e}"})


@app.command("sell")
def sell(
    coin: str = typer.Argument(..., help="Token to sell (e.g. HYPE, ETH, BTC)"),
    amount: float = typer.Option(0, "--amount", help="Token amount to sell (0 = all)"),
    pct: float = typer.Option(0, "--pct", help="Percentage to sell (e.g. 50 = half)"),
) -> None:
    """Sell a token from spot holdings."""
    from rift.trading_gates import require_trading_ready
    from rift.builder_fee import get_builder_info

    result = require_trading_ready()
    if result is None:
        return
    private_key, account_address = result

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account

    base_url = constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    wallet = Account.from_key(private_key)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    # Resolve user-facing name → HL @N pair + token name + price + sz_decimals.
    # spot_user_state.balances is keyed by HL token name (UBTC, not BTC),
    # so the resolved `token` name must be used for the holdings lookup
    # below too — not the raw user input.
    try:
        pair, token, price, sz_decimals = _resolve_spot_pair(info, coin)
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        return

    # Get current holdings
    spot_state = info.spot_user_state(account_address)
    balances = spot_state.get("balances", [])
    holding = next((b for b in balances if b.get("coin") == token), None)

    if not holding or float(holding.get("total", 0)) <= 0:
        _emit({"type": "error", "msg": f"No {token} holdings to sell."})
        return

    total_held = float(holding["total"])

    if amount > 0:
        sell_size = min(amount, total_held)
    elif pct > 0:
        sell_size = total_held * (pct / 100.0)
    else:
        sell_size = total_held  # sell all
    # Round DOWN to szDecimals so we never try to sell more than HL allows.
    # Plain round() would round-half-up which on the "sell all" path could
    # exceed total_held by an ulp and trigger an "insufficient balance" error.
    import math
    factor = 10 ** sz_decimals
    sell_size = math.floor(sell_size * factor) / factor
    if sell_size <= 0:
        _emit({"type": "error",
               "msg": f"Computed sell size rounds to zero at szDecimals={sz_decimals}. "
                      f"Holdings too small to sell at HL precision."})
        return

    builder_info = get_builder_info("spot")
    _emit({"type": "progress", "pct": 50, "msg": f"Selling {sell_size} {token} at ~${price:.4f}..."})

    try:
        resp = exchange.market_open(pair, is_buy=False, sz=sell_size, builder=builder_info)
        order_err = _hl_order_error(resp)
        if order_err:
            _emit({"type": "error", "msg": f"Sell rejected by Hyperliquid: {order_err}"})
            return
        _emit({
            "type": "result", "command": "sell", "market": "spot",
            "token": token, "pair": pair, "size": round(sell_size, 8),
            "price": price, "total_value": round(sell_size * price, 2),
            "builder_fee": f"{BUILDER_FEE_DISPLAY_SPOT} (sell side)",
            "response": resp,
        })
    except Exception as e:
        _emit({"type": "error", "msg": f"Sell failed: {e}"})


from rift.builder_fee import BUILDER_FEE_DISPLAY_SPOT  # noqa: E402


@app.command("holdings")
def holdings() -> None:
    """View spot token holdings and current values."""
    from rift.trading_gates import get_api_key, get_account_address

    account = get_account_address()
    if not account:
        _emit({"type": "error", "msg": "No wallet configured. Run: rift auth setup --key 0x..."})
        return

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    spot_state = info.spot_user_state(account)
    balances = spot_state.get("balances", [])
    mids = info.all_mids()

    result_holdings = []
    total_value = 0.0

    for b in balances:
        total_amount = float(b.get("total", 0))
        if total_amount <= 0:
            continue
        coin = b["coin"]
        entry_ntl = float(b.get("entryNtl", 0))

        if coin == "USDC":
            price = 1.0
        else:
            pair = f"{coin}/USDC"
            price = float(mids.get(pair, 0))

        value = total_amount * price
        total_value += value
        pnl_pct = ((value - entry_ntl) / entry_ntl * 100) if entry_ntl > 0 else 0.0

        result_holdings.append({
            "coin": coin, "amount": round(total_amount, 6),
            "price": round(price, 4), "value_usd": round(value, 2),
            "entry_cost": round(entry_ntl, 2), "pnl_pct": round(pnl_pct, 1),
        })

    result_holdings.sort(key=lambda h: h["value_usd"], reverse=True)
    _emit({
        "type": "result", "command": "holdings",
        "holdings": result_holdings,
        "total_value_usd": round(total_value, 2),
    })


@app.command("balance")
def balance() -> None:
    """Show combined spot and perps wallet balances."""
    from rift.trading_gates import get_api_key, get_account_address

    account = get_account_address()
    if not account:
        _emit({"type": "error", "msg": "No wallet configured. Run: rift auth setup --key 0x..."})
        return

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    # Single source of truth for mode + perp + USDC reading. Computing
    # tradeable here is wrong — read_collateral already does it correctly
    # per mode (including PM's trust-HL-accountValue behavior).
    from rift_data.account_mode import read_collateral
    perp_state = info.user_state(account)
    c = read_collateral(info, account, perp_state=perp_state)
    mode = c.mode
    perp_equity = float(c.perp_account_value)
    perp_in_positions = float(c.perp_margin_used)
    perp_available = float(c.perp_available)
    spot_usdc = float(c.spot_usdc)

    # Spot breakdown — read_collateral only tracks USDC; for the
    # balance display we also want the value of non-USDC tokens.
    spot_state = info.spot_user_state(account)
    mids = info.all_mids()
    spot_total = 0.0
    for b in spot_state.get("balances", []):
        total_amount = float(b.get("total", 0))
        if total_amount <= 0:
            continue
        coin = b["coin"]
        if coin == "USDC":
            spot_total += total_amount
        else:
            price = float(mids.get(f"{coin}/USDC", 0))
            spot_total += total_amount * price

    _emit({
        "type": "result", "command": "balance",
        "account_mode": mode,
        "perps": {"equity": round(perp_equity, 2), "available": round(perp_available, 2),
                  "in_positions": round(perp_in_positions, 2)},
        "spot": {"total_value": round(spot_total, 2), "usdc": round(spot_usdc, 2),
                 "tokens": round(spot_total - spot_usdc, 2)},
        "tradeable_collateral": round(float(c.total), 2),
        "total": round(perp_equity + spot_total, 2),
    })

    # Standard-mode users whose USDC sits in spot can't trade perps until
    # they transfer. Common after a Unified→Standard mode switch (HL leaves
    # the consolidated USDC in spot) or after first deposit before allocation.
    # Hint at the fix instead of leaving the user to guess.
    if mode == "standard" and perp_equity < 10 and spot_usdc >= 10:
        suggested = int(spot_usdc * 0.9)  # leave 10% headroom in spot
        _hint(
            f"You have ${spot_usdc:.2f} in spot but ${perp_equity:.2f} in perp. "
            f"Transfer to start trading: rift trade transfer {suggested} --direction to-perps"
        )


@app.command("transfer")
def transfer(
    amount: float = typer.Argument(..., help="USDC amount to transfer"),
    direction: str = typer.Option("to-perps", "--direction", help="to-perps or to-spot"),
) -> None:
    """Transfer USDC between spot and perps wallets.

    Requires main wallet key (API wallets cannot transfer).
    """
    from rift.config import get_env_var

    # Transfer needs main wallet, not API wallet
    private_key = get_env_var("HYPERLIQUID_PRIVATE_KEY")
    if not private_key:
        _emit({"type": "error", "msg": "No wallet configured. Run: rift auth setup --key 0x..."})
        return

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account

    base_url = constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    wallet = Account.from_key(private_key)
    exchange = Exchange(wallet, base_url)

    # Mode-aware: under unified/PM HL rejects usd_class_transfer outright
    # ("Action disabled when unified account is active"). Surface a clear
    # message instead of forwarding the cryptic HL error.
    from rift_data.account_mode import query_account_mode
    mode = query_account_mode(info, wallet.address)
    if mode != "standard":
        _emit({
            "type": "result", "command": "transfer",
            "amount": amount, "direction": direction,
            "skipped": True,
            "mode": mode,
            "msg": (
                f"Account is in {mode} mode — spot USDC is already perp collateral, "
                "no transfer needed. To switch to separate spot/perp balances: "
                "rift account-mode-set standard --local-main-key <key>"
            ),
        })
        return

    to_perp = direction == "to-perps"
    label = "Spot → Perps" if to_perp else "Perps → Spot"

    _emit({"type": "progress", "pct": 50, "msg": f"Transferring ${amount} ({label})..."})

    try:
        resp = exchange.usd_class_transfer(amount, to_perp)
        # Same silent-err pattern as builder fee: SDK does not raise on err
        if isinstance(resp, dict) and resp.get("status") != "ok":
            _emit({"type": "error", "msg": f"Hyperliquid rejected transfer: {resp.get('response')}", "hl_response": resp})
            return
        _emit({
            "type": "result", "command": "transfer",
            "amount": amount, "direction": label,
            "response": resp,
        })
        _hint("Check balances with 'rift balance'")
    except Exception as e:
        error_msg = str(e)
        if "agent" in error_msg.lower() or "not authorized" in error_msg.lower():
            _emit({"type": "error", "msg": "Transfer requires main wallet key, not API wallet. Use Hyperliquid UI to transfer."})
        else:
            _emit({"type": "error", "msg": f"Transfer failed: {e}"})


# ─── AI AGENT INTELLIGENCE LAYER ────────────────────────────


@app.command("state")
def project_state() -> None:
    """Full project snapshot — strategies, sessions, edge, lessons, alerts, auth, data."""
    from rift.strategy import discover_strategies as _ds, list_strategies as _ls

    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies", Path(__file__).parent / "strategies"]
    from rift.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    _ds(dirs)

    strategies = [{"name": n, "class": c.__name__, "interval": c.default_interval} for n, c in _ls().items()]

    try:
        from rift.algo import list_algo_sessions
        algo = list_algo_sessions()
    except Exception:
        algo = []

    edge = {}
    edge_path = Path.home() / ".rift" / "validated_edge.json"
    if edge_path.exists():
        try:
            edge = json.loads(edge_path.read_text())
        except Exception:
            pass

    lessons = []
    lessons_path = Path.home() / ".rift" / "lessons.json"
    if lessons_path.exists():
        try:
            lessons = json.loads(lessons_path.read_text())[-10:]
        except Exception:
            pass

    try:
        from rift.alerts import get_recent_alerts
        alerts = get_recent_alerts(5)
    except Exception:
        alerts = []

    from rift.trading_gates import get_api_key, get_account_address
    auth_configured = bool(get_api_key())

    from rift.data import list_cached_data
    cached = list_cached_data()
    coins_available = sorted(set(d.get("pair", "") for d in cached))

    _emit({
        "type": "result", "command": "state",
        "strategies": strategies,
        "algo_sessions": algo,
        "validated_edge": edge.get("validated_edge", {}),
        "recent_lessons": lessons,
        "recent_alerts": alerts,
        "auth": {"configured": auth_configured,
                 "account": get_account_address() if auth_configured else ""},
        "data": {"coins": len(coins_available), "datasets": len(cached),
                 "coins_list": coins_available},
    })


@app.command("scan")
def feature_scan(
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", help="Timeframe"),
    forward: str = typer.Option("4h", "--forward", help="Forward return horizon"),
) -> None:
    """Scan all indicators for predictive power against forward returns."""
    import numpy as np
    from rift.data import normalize_coin
    from rift.historical_data import load_candles_smart
    from rift.backtest import _compute_indicator
    from rift.strategy import RSI, EMA, ATR, ADX, BBWidth, VolRatio, MACD, StochK, CCI, ROC, CMF, WilliamsR, KAMA

    coin = normalize_coin(pair)
    df = load_candles_smart(coin, interval)
    if df is None or len(df) < 200:
        _emit({"type": "error", "msg": f"Not enough data for {coin} {interval} (need 200+ candles)"})
        return

    closes = df["close"].to_numpy().astype(float)
    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    volumes = df["volume"].to_numpy().astype(float)
    timestamps = df["timestamp"].to_numpy()
    n = len(closes)

    fwd_candles = max(1, _parse_duration(forward) // _parse_duration(interval))
    fwd_returns = np.full(n, np.nan)
    for i in range(n - fwd_candles):
        fwd_returns[i] = (closes[i + fwd_candles] - closes[i]) / closes[i]

    indicators_to_test = {
        "rsi_14": RSI(14), "rsi_7": RSI(7),
        "ema_12": EMA(12), "ema_26": EMA(26), "ema_50": EMA(50),
        "atr_14": ATR(14), "adx_14": ADX(14),
        "bb_width": BBWidth(20), "vol_ratio": VolRatio(20),
        "macd": MACD(12, 26, 9), "stoch_k": StochK(14),
        "cci_20": CCI(20), "roc_12": ROC(12),
        "cmf_20": CMF(20), "williams_r": WilliamsR(14),
        "kama_10": KAMA(10),
    }

    results = []
    for ind_name, ind in indicators_to_test.items():
        values = _compute_indicator(ind_name, ind, closes, highs, lows, volumes, timestamps, interval)
        valid = ~(np.isnan(values) | np.isnan(fwd_returns))
        if np.sum(valid) < 100:
            continue
        # Rank correlation (Spearman)
        v_vals = values[valid]
        f_vals = fwd_returns[valid]
        # Manual Spearman (avoid scipy dependency)
        from scipy.stats import spearmanr
        try:
            ic, p_value = spearmanr(v_vals, f_vals)
        except Exception:
            # Fallback: numpy rank correlation
            r_v = np.argsort(np.argsort(v_vals)).astype(float)
            r_f = np.argsort(np.argsort(f_vals)).astype(float)
            ic = float(np.corrcoef(r_v, r_f)[0, 1])
            p_value = 0.05  # approximate
        results.append({
            "indicator": ind_name,
            "ic": round(float(ic), 4),
            "abs_ic": round(abs(float(ic)), 4),
            "p_value": round(float(p_value), 4),
            "significant": bool(p_value < 0.05),
            "direction": "positive" if ic > 0 else "negative",
            "samples": int(np.sum(valid)),
        })

    results.sort(key=lambda x: x["abs_ic"], reverse=True)
    _emit({
        "type": "result", "command": "scan",
        "pair": coin, "interval": interval, "forward_horizon": forward,
        "candles_analyzed": n, "features": results, "top_3": results[:3],
    })


@app.command("algo")
def algo(
    pair: str = typer.Option("BTC-PERP", "--pair", help="Trading pair"),
    equity: float = typer.Option(0, "--equity", help="Starting equity (0 = auto-detect)"),
    account_address: str = typer.Option("", "--account", help="Main account address"),
    daemon: bool = typer.Option(False, "--daemon", help="Run as background daemon"),
    strategy_override: str = typer.Option("", "--strategy", help="Force a specific strategy (auto-detect if empty)"),
    size_usd_override: float = typer.Option(
        0.0,
        "--size-usd",
        help=(
            "Override the strategy's risk-model-derived position size with a "
            "fixed USD notional. Intended for small-account setup verification "
            "(most strategies size 1-20% of equity, below HL's $10 minimum on "
            "small balances). Volume cap and per-strategy gate still apply. "
            "Default 0 = no override."
        ),
    ),
) -> None:
    """Start algo trading — auto-discovers the best strategy for your coin.

    Scans registered strategies, finds the one that handles your coin,
    and runs it at the correct timeframe. Strategy-agnostic.

    For manual one-off trades, use: rift scout → rift recon
    """
    from pathlib import Path
    from rift.strategy import discover_strategies as _discover, list_strategies, get_strategy

    _discover([Path(__file__).parent.parent.parent.parent.parent / "strategies", Path(__file__).parent / "strategies"])

    from rift.data import normalize_coin
    coin = normalize_coin(pair)

    # Auto-discover which strategy handles this coin
    if strategy_override:
        strategy_name = strategy_override
        try:
            strategy_cls = get_strategy(strategy_name)
        except KeyError:
            _emit({"type": "error", "msg": f"Strategy '{strategy_name}' not found."})
            return
    else:
        # Scan all registered strategies for one that has this coin in COIN_CONFIGS
        all_strats = list_strategies()
        strategy_name = None
        strategy_cls = None

        for name, cls in all_strats.items():
            # Check if strategy module has COIN_CONFIGS with this coin
            module = sys.modules.get(cls.__module__)
            if module and hasattr(module, 'COIN_CONFIGS'):
                coin_configs = getattr(module, 'COIN_CONFIGS')
                if coin in coin_configs:
                    strategy_name = name
                    strategy_cls = cls
                    break

        if strategy_name is None:
            _emit({"type": "error", "msg": f"No strategy found for {coin}. Add a strategy with {coin} in its COIN_CONFIGS."})
            return

    # Get the strategy's preferred timeframe
    instance = strategy_cls()
    interval = instance.default_interval

    # Preflight: verify cross-asset data is available
    from rift.strategy import preflight_check_data
    preflight_errors = preflight_check_data(instance, interval)
    if preflight_errors:
        for e in preflight_errors:
            _emit({"type": "error", "msg": e})
        return

    _emit({"type": "info", "msg": f"ALGO MODE — {strategy_name} on {coin} ({interval})"})

    from rift.algo import run_algo
    from rift.trading_gates import require_trading_ready

    result = require_trading_ready()
    if result is None:
        return
    private_key, gate_account = result
    if not account_address:
        account_address = gate_account

    run_algo(
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        initial_equity=equity,
        private_key=private_key,
        account_address=account_address,
        daemon=daemon,
        size_usd_override=size_usd_override,
    )


@app.command("algo-status")
def algo_status(
    strategy: str = typer.Option("", "--strategy", help="Strategy name (omit for all)"),
    pair: str = typer.Option("", "--pair", help="Trading pair"),
) -> None:
    """Get status of running algo trading sessions."""
    from rift.algo import list_algo_sessions, get_algo_session

    if strategy and pair:
        session = get_algo_session(strategy, pair)
        if session:
            _emit({"type": "result", "command": "algo-status", "session": session})
        else:
            _emit({"type": "result", "command": "algo-status", "session": None, "msg": "No running session found"})
    else:
        sessions = list_algo_sessions()
        _emit({"type": "result", "command": "algo-status", "sessions": sessions})


@app.command("algo-stop")
def algo_stop(
    strategy: str = typer.Option(..., "--strategy", help="Strategy name"),
    pair: str = typer.Option("BTC-PERP", "--pair", help="Trading pair"),
) -> None:
    """Stop a running algo trading session."""
    from rift.algo import stop_algo_session

    result = stop_algo_session(strategy, pair)
    _emit({"type": "result", "command": "algo-stop", **result})


def _require_running_session(strategy: str, pair: str) -> str:
    """Return the algo session key if a daemon is currently running, else
    emit an error and exit non-zero.

    Without this check, close-position / tighten-stop / reduce-position
    would silently write a command file to ~/.rift/algo/commands/<key>.json
    even when no daemon exists for that key — leaving a stale file that
    could be picked up later by a daemon spawned with a colliding key. The
    preflight forces the caller to confirm the session exists before any
    side effect occurs.
    """
    from rift.data import normalize_coin
    from rift.algo import get_algo_session
    coin = normalize_coin(pair)
    key = f"{strategy}_{coin}"
    session = get_algo_session(strategy, pair)
    if session is None:
        _emit({
            "type": "error",
            "msg": f"No running algo session for '{key}'. "
                   f"Start one with `rift algo {strategy} --pair {pair}` first, "
                   f"or check `rift algo status` to see what's active.",
        })
        sys.exit(1)
    return key


@app.command("close-position")
def close_position(
    strategy: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC-PERP", "--pair", help="Trading pair"),
) -> None:
    """Close position on a running algo session."""
    key = _require_running_session(strategy, pair)
    cmd_dir = Path.home() / ".rift" / "algo" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / f"{key}.json").write_text(json.dumps({"action": "close"}))
    _emit({"type": "result", "command": "close-position", "status": "command_sent", "key": key})


@app.command("tighten-stop")
def tighten_stop(
    strategy: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC-PERP", "--pair", help="Trading pair"),
    price: float = typer.Option(..., "--price", help="New stop loss price"),
) -> None:
    """Tighten stop loss on a running algo session."""
    if price <= 0:
        _emit({"type": "error", "msg": f"--price must be > 0 (got {price})."})
        sys.exit(1)
    key = _require_running_session(strategy, pair)
    cmd_dir = Path.home() / ".rift" / "algo" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / f"{key}.json").write_text(json.dumps({"action": "tighten_stop", "price": price}))
    _emit({"type": "result", "command": "tighten-stop", "status": "command_sent", "key": key, "price": price})


@app.command("reduce-position")
def reduce_position(
    strategy: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC-PERP", "--pair", help="Trading pair"),
    pct: float = typer.Option(50.0, "--pct", help="Percentage to close (50 = close half)"),
) -> None:
    """Reduce position size on a running algo session."""
    if pct <= 0 or pct > 100:
        _emit({"type": "error", "msg": f"--pct must be in (0, 100] (got {pct})."})
        sys.exit(1)
    key = _require_running_session(strategy, pair)
    cmd_dir = Path.home() / ".rift" / "algo" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / f"{key}.json").write_text(json.dumps({"action": "reduce", "pct": pct / 100.0}))
    _emit({"type": "result", "command": "reduce-position", "status": "command_sent", "key": key, "pct": pct})


def _show_recon_history(n: int = 20) -> None:
    """Read recon trade logs and emit as NDJSON result."""
    from pathlib import Path

    trades = []
    d = Path.home() / ".rift" / "recon"
    if d.exists():
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                trades.append(data)
            except Exception:
                pass

    trades.sort(key=lambda t: t.get("started_at", ""), reverse=True)
    trades = trades[:n]

    summaries = []
    for t in trades:
        summaries.append({
            "date": t.get("started_at", "")[:16],
            "coin": t.get("coin", ""),
            "direction": t.get("direction", ""),
            "pnl_pct": t.get("pnl_pct", 0),
            "pnl_usd": t.get("pnl_usd", 0),
            "exit_reason": t.get("exit_reason", ""),
            "hold_minutes": t.get("hold_minutes", 0),
            "confidence": t.get("confidence_tier", ""),
            "score": t.get("score", 0),
            "execution_grade": t.get("execution_grade", ""),
            "leverage": t.get("leverage", 1),
        })

    # Summary stats
    if summaries:
        total_pnl = sum(t["pnl_usd"] for t in summaries)
        wins = sum(1 for t in summaries if t["pnl_pct"] > 0)
        win_rate = wins / len(summaries) * 100
    else:
        total_pnl = 0
        win_rate = 0

    _emit({
        "type": "result", "command": "recon-history",
        "trades": summaries,
        "total": len(summaries),
        "total_pnl_usd": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
    })


@app.command("recon")
def recon_cmd(
    top: int = typer.Option(20, "--top", help="Number of coins to scan"),
    bias_tf: str = typer.Option("1h", "--bias-tf", help="Higher timeframe for bias"),
    entry_tf: str = typer.Option("5m", "--entry-tf", help="Lower timeframe for entry"),
    min_confluence: int = typer.Option(2, "--min", help="Minimum signals"),
    soak: int = typer.Option(120, "--soak", help="Soak seconds (0 = skip)"),
    no_soak: bool = typer.Option(False, "--no-soak", help="Skip websocket soak"),
    confirm: int = typer.Option(120, "--confirm", help="Tape confirmation window in seconds. Tape stats only populate after the first 60s flush, so values below ~65 are guaranteed to abort with 'tape_not_confirmed'. Default 120 leaves a full second flush window."),
    auto: int = typer.Option(0, "--auto", help="Auto-pick the Nth opportunity (0 = interactive)"),
    no_guard: bool = typer.Option(False, "--no-guard", help="Skip correlation guard"),
    history: int = typer.Option(0, "--history", help="Show last N recon trades (default 20)"),
    account_address: str = typer.Option("", "--account", help="Main wallet address"),
    size_usd_override: float = typer.Option(
        0.0,
        "--size-usd",
        help=(
            "Override scout's computed position size with a fixed USD value. "
            "Intended for small-account setup verification (default Kelly sizing "
            "is typically <1% of equity, below HL's $10 minimum on small balances). "
            "Volume cap and correlation guard still apply. The override is "
            "announced loudly in the output. Default 0 = no override."
        ),
    ),
) -> None:
    """Scout the market, pick a trade, and execute with tape confirmation."""
    # Handle --history early return
    if history > 0 or "--history" in sys.argv:
        _show_recon_history(history if history > 0 else 20)
        return

    # Tape confirmation needs at least one bucket flush to have any data.
    # LiveMarketFeed buckets are 60s; the first flush happens just after T+60.
    # Anything below that window is guaranteed to abort. Warn loudly so the
    # user knows their value is below the practical floor before we burn
    # 60 seconds of wall-clock waiting on a tape that will never confirm.
    if 0 < confirm < 65:
        _emit({
            "type": "warning",
            "msg": f"--confirm {confirm} is below the 65-second tape-aggregation floor; "
                   f"this run will almost certainly abort with 'tape_not_confirmed'. "
                   f"Pass --confirm 120 (default) or higher for real confirmation, "
                   f"or accept this run as a no-op smoke test.",
        })

    from rift.scout import scan_market
    from rift.recon import run_recon
    import dataclasses

    soak_seconds = 0 if no_soak else soak

    # Phase 1: Scout scan
    opportunities = scan_market(
        top_n=top, bias_tf=bias_tf, entry_tf=entry_tf,
        min_confluence=min_confluence, soak_seconds=soak_seconds,
    )

    if not opportunities:
        _emit({"type": "result", "command": "recon", "msg": "No opportunities found", "opportunities": []})
        return

    # Emit results for NDJSON consumers
    _emit({
        "type": "result", "command": "recon",
        "opportunities": [dataclasses.asdict(o) for o in opportunities],
        "scanned": top,
    })

    # Phase 2: Pick
    selected = None

    if auto > 0:
        if auto <= len(opportunities):
            selected = opportunities[auto - 1]
        else:
            _emit({"type": "error", "msg": f"--auto {auto} but only {len(opportunities)} opportunities"})
            return
    else:
        # Interactive picker — menu to stderr, input from stdin
        import sys as _sys
        print("\n  SCOUT RESULTS\n", file=_sys.stderr)
        for i, opp in enumerate(opportunities, 1):
            print(
                f"  [{i}]  {opp.direction:5s} {opp.coin:<8s}  "
                f"score={opp.score:.3f}  {opp.confidence_tier:<6s}  "
                f"{opp.leverage}x  size={opp.size_pct*100:.1f}%  "
                f"hold={opp.hold_type}  cats={opp.num_categories}  "
                f"funding={opp.funding_rate*100:+.3f}%",
                file=_sys.stderr,
            )
        print(f"\n  Pick [1-{len(opportunities)}] or q to quit: ", end="", file=_sys.stderr, flush=True)

        try:
            choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice.lower() == "q" or not choice:
            return

        try:
            pick = int(choice)
            if 1 <= pick <= len(opportunities):
                selected = opportunities[pick - 1]
            else:
                _emit({"type": "error", "msg": f"Invalid pick: {pick}"})
                return
        except ValueError:
            _emit({"type": "error", "msg": f"Invalid input: {choice}"})
            return

    # Phase 3: Trading gates
    from rift.trading_gates import require_trading_ready

    result = require_trading_ready()
    if result is None:
        return
    private_key, gate_account = result

    if not account_address:
        account_address = gate_account

    _emit({"type": "status", "msg": f"Executing RECON: {selected.direction} {selected.coin} ({selected.confidence_tier})"})

    run_recon(
        opportunity=selected,
        private_key=private_key,
        account_address=account_address,
        confirm_seconds=confirm,
        no_guard=no_guard,
        size_usd_override=size_usd_override,
    )


@app.command("manual-trade")
def manual_trade_cmd(
    coin: str = typer.Argument(..., help="Coin (e.g. BTC, ETH, SOL)"),
    side: str = typer.Argument(..., help="Direction: long or short"),
    size_usd: float = typer.Option(500, "--size", help="Position size in USD"),
    stop_pct: float = typer.Option(0.02, "--stop", help="Stop loss percentage (0.02 = 2%)"),
    leverage: int = typer.Option(1, "--leverage", help="Leverage"),
    account_address: str = typer.Option("", "--account", help="Account address"),
) -> None:
    """Place a manual trade with stop loss and monitor live."""
    from rift.manual_trade import run_manual_trade
    from rift.trading_gates import require_trading_ready

    result = require_trading_ready()
    if result is None:
        return
    private_key, gate_account = result
    if not account_address:
        account_address = gate_account

    run_manual_trade(
        coin=coin, side=side, size_usd=size_usd,
        stop_pct=stop_pct, leverage=leverage,
        private_key=private_key, account_address=account_address,

    )


@app.command("test-trade")
def test_trade(
    account_address: str = typer.Option("", "--account", help="Main wallet address"),
) -> None:
    """Place a minimum-size test trade to verify exchange connectivity."""
    import os

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from eth_account import Account
    from rift.builder_fee import get_builder_info
    from rift.algo import _load_sz_decimals, _round_size, _round_price

    from rift.config import get_env_var
    private_key = get_env_var("HYPERLIQUID_PRIVATE_KEY")
    if not private_key:
        _emit({"type": "error", "msg": "No API key found. Run: rift auth setup"})
        sys.exit(1)

    base_url = constants.MAINNET_API_URL
    builder_info = get_builder_info()

    wallet = Account.from_key(private_key)
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    coin = "BTC"
    results = {
        "success": False,
        "entry_price": 0,
        "exit_price": 0,
        "pnl": 0,
        "stop_placed": False,
        "close_success": False,
    }

    try:
        # Step 1: Check account collateral (mode-aware — unified-account
        # users have spot USDC as their perp margin pool)
        _emit({"type": "status", "msg": "  Connecting to Hyperliquid..."})
        from rift_data.account_mode import read_collateral
        collateral = read_collateral(info, account_address)
        balance = float(collateral.total)
        _emit({"type": "status", "msg": f"  ✔ Connected — Balance: ${balance:,.2f} ({collateral.mode} mode)"})

        if balance < 10:
            _emit({"type": "error", "msg": f"Need at least $10. Current balance: ${balance:.2f}"})
            _emit({"type": "result", "success": False, "error": "Insufficient balance"})
            return

        # Step 2: Get price and set leverage
        sz_map = _load_sz_decimals(info)
        sz_dec = sz_map.get(coin, 2)
        mids = info.all_mids()
        price = float(mids.get(coin, "0"))
        _emit({"type": "status", "msg": f"  ✔ BTC price: ${price:,.2f} (szDecimals={sz_dec})"})

        exchange.update_leverage(2, coin, is_cross=True)
        _emit({"type": "status", "msg": "  ✔ Leverage set to 2x"})

        # Step 3: Place entry + stop loss atomically
        size = _round_size(10.0 / price, sz_dec)  # ~$10 notional
        slippage = 0.01
        entry_px = _round_price(price * (1 + slippage), sz_dec)
        stop_price = _round_price(price * 0.95, sz_dec)  # 5% stop

        _emit({"type": "status", "msg": f"  Placing BTC long: {size} @ ~${price:,.0f} with stop @ ${stop_price:,.0f}..."})

        orders = [
            {
                "coin": coin, "is_buy": True, "sz": size, "limit_px": entry_px,
                "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": False,
            },
            {
                "coin": coin, "is_buy": False, "sz": size, "limit_px": stop_price,
                "order_type": {"trigger": {"triggerPx": stop_price, "isMarket": True, "tpsl": "sl"}},
                "reduce_only": True,
            },
        ]

        result = exchange.bulk_orders(orders, grouping="normalTpsl", builder=builder_info)

        if result.get("status") != "ok":
            _emit({"type": "error", "msg": f"Order failed: {result}"})
            _emit({"type": "result", "success": False, "error": f"Order rejected: {result}"})
            return

        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        entry_oid = None
        stop_oid = None
        fill_price = price

        for s in statuses:
            if "error" in s:
                _emit({"type": "error", "msg": f"Order error: {s['error']}"})
                _emit({"type": "result", "success": False, "error": s["error"]})
                return
            elif "filled" in s:
                entry_oid = s["filled"].get("oid")
                fill_price = float(s["filled"].get("avgPx", price))
            elif "resting" in s:
                stop_oid = s["resting"].get("oid")

        if entry_oid is None:
            _emit({"type": "error", "msg": "Entry order did not fill"})
            if stop_oid:
                exchange.cancel(coin, stop_oid)
            _emit({"type": "result", "success": False, "error": "Entry not filled"})
            return

        results["entry_price"] = round(fill_price, 2)
        results["stop_placed"] = stop_oid is not None
        _emit({"type": "status", "msg": f"  ✔ Entry filled @ ${fill_price:,.2f} (oid={entry_oid})"})
        _emit({"type": "status", "msg": f"  ✔ Stop loss {'placed' if stop_oid else 'MISSING'} @ ${stop_price:,.0f} (oid={stop_oid})"})

        # Step 4: Verify position on exchange
        _emit({"type": "status", "msg": "  Verifying position on exchange..."})
        time.sleep(2)
        state = info.user_state(account_address)
        pos_found = False
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                szi = float(pos.get("szi", "0"))
                if szi != 0:
                    pos_found = True
                    _emit({"type": "status", "msg": f"  ✔ Position confirmed: {szi} BTC"})
        if not pos_found:
            _emit({"type": "status", "msg": "  ⚠ Position not found on exchange (may have been stopped out)"})

        # Step 5: Wait
        _emit({"type": "status", "msg": "  Holding for 10 seconds..."})
        time.sleep(10)

        # Step 6: Close position
        _emit({"type": "status", "msg": "  Closing position..."})

        # Cancel stop loss first
        if stop_oid:
            try:
                exchange.cancel(coin, stop_oid)
            except Exception:
                pass

        # Market close
        close_price = float(info.all_mids().get(coin, "0"))
        close_px = _round_price(close_price * (1 - slippage), sz_dec)
        close_result = exchange.order(
            coin, False, size, close_px,
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=True, builder=builder_info,
        )

        exit_price = close_price
        if close_result.get("status") == "ok":
            cs = close_result.get("response", {}).get("data", {}).get("statuses", [])
            if cs and "filled" in cs[0]:
                exit_price = float(cs[0]["filled"].get("avgPx", close_price))
                results["close_success"] = True

        results["exit_price"] = round(exit_price, 2)
        pnl = size * (exit_price - fill_price)
        results["pnl"] = round(pnl, 4)
        results["success"] = True

        _emit({"type": "status", "msg": f"  ✔ Position closed @ ${exit_price:,.2f}"})
        _emit({"type": "status", "msg": f"  ✔ P&L: ${pnl:,.4f}"})

        # Verify clean
        time.sleep(1)
        state = info.user_state(account_address)
        still_open = False
        for ap in state.get("assetPositions", []):
            if ap.get("position", {}).get("coin") == coin:
                if float(ap["position"].get("szi", "0")) != 0:
                    still_open = True
        if not still_open:
            _emit({"type": "status", "msg": "  ✔ No residual positions"})
        else:
            _emit({"type": "status", "msg": "  ⚠ Position may still be open — check Hyperliquid UI"})

    except Exception as e:
        _emit({"type": "error", "msg": f"Test failed: {e}"})
        results["error"] = str(e)

    _emit({"type": "result", **results})


@app.command("close-all")
def close_all(
    coin: str = typer.Option("", "--coin", help="Coin to close (empty = all)"),
    account_address: str = typer.Option("", "--account", help="Main account address"),
) -> None:
    """Emergency: close all positions and cancel all orders."""
    import os
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    # SECURITY: Read private key from env, not CLI args
    from rift.config import get_env_var
    private_key = get_env_var("HYPERLIQUID_PRIVATE_KEY")
    from eth_account import Account

    if not private_key:
        _emit({"type": "error", "msg": "No API wallet key provided"})
        sys.exit(1)

    base_url = constants.MAINNET_API_URL
    wallet = Account.from_key(private_key)
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url, account_address=account_address)

    # Cancel all open orders
    try:
        open_orders = info.open_orders(account_address)
        for order in open_orders:
            try:
                exchange.cancel(order["coin"], order["oid"])
                _emit({"type": "status", "msg": f"Cancelled order {order['oid']} on {order['coin']}"})
            except Exception as e:
                _emit({"type": "error", "msg": f"Cancel failed: {e}"})
    except Exception as e:
        _emit({"type": "error", "msg": f"Cannot fetch orders: {e}"})

    # Close all positions
    try:
        state = info.user_state(account_address)
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            pos_coin = pos.get("coin", "")
            szi = float(pos.get("szi", "0"))

            if szi == 0:
                continue
            if coin and pos_coin != coin:
                continue

            is_buy = szi < 0  # buy to close short
            size = abs(szi)

            try:
                mid = float(info.all_mids().get(pos_coin, "0"))
                slippage = 0.01  # 1% slippage for emergency close
                limit_px = round(mid * (1 + slippage) if is_buy else mid * (1 - slippage), 2)

                result = exchange.order(
                    pos_coin, is_buy, size, limit_px,
                    order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=True,
                )
                _emit({"type": "status", "msg": f"Closed {pos_coin}: {'bought' if is_buy else 'sold'} {size}"})
            except Exception as e:
                _emit({"type": "error", "msg": f"Close {pos_coin} failed: {e}"})
    except Exception as e:
        _emit({"type": "error", "msg": f"Cannot fetch positions: {e}"})

    _emit({"type": "result", "command": "close-all", "status": "done"})


if __name__ == "__main__":
    app()


