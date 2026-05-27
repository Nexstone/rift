"""Scout / lessons / signals / audit commands — extracted from cli.py in Phase 6.

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


def _record_lesson(coin: str, strategy: str, approach: str, result: str, reason: str, metrics: dict | None = None) -> None:
    """Append a lesson to ~/.rift/lessons.json."""

    lessons_path = Path.home() / ".rift" / "lessons.json"
    lessons = []
    if lessons_path.exists():
        try:
            lessons = json.loads(lessons_path.read_text())
        except Exception:
            pass
    lessons.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "coin": coin, "strategy": strategy, "approach": approach,
        "result": result, "reason": reason, "metrics": metrics or {},
    })
    Path.home().joinpath(".rift").mkdir(parents=True, exist_ok=True)
    lessons_path.write_text(json.dumps(lessons, indent=2))


@app.command("lessons")
def lessons_cmd(
    coin: str = typer.Option("", "--coin", help="Filter by coin"),
    strategy: str = typer.Option("", "--strategy", help="Filter by strategy"),
    limit: int = typer.Option(20, "--limit", help="Number of lessons"),
) -> None:
    """Query lessons learned from past research and trading."""
    lessons_path = Path.home() / ".rift" / "lessons.json"
    lessons = []
    if lessons_path.exists():
        try:
            lessons = json.loads(lessons_path.read_text())
        except Exception:
            pass
    if coin:
        lessons = [l for l in lessons if coin.upper() in l.get("coin", "").upper()]
    if strategy:
        lessons = [l for l in lessons if strategy in l.get("strategy", "")]
    lessons = lessons[-limit:]
    _emit({"type": "result", "command": "lessons", "lessons": lessons, "total": len(lessons)})


@app.command("add-lesson")
def add_lesson(
    coin: str = typer.Option(..., "--coin", help="Coin tested"),
    approach: str = typer.Option(..., "--approach", help="What was tried"),
    result: str = typer.Option(..., "--result", help="pass or fail"),
    reason: str = typer.Option("", "--reason", help="Why it passed/failed"),
) -> None:
    """Manually record a lesson learned."""
    _record_lesson(coin=coin, strategy="manual", approach=approach, result=result, reason=reason)
    _emit({"type": "result", "command": "add-lesson", "status": "recorded"})


@app.command("verify")
def verify(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    start: str = typer.Option("", "--from", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option("", "--to", help="End date (YYYY-MM-DD)"),
    interval: str = typer.Option("", "--tf", help="Timeframe"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategies directory"),
) -> None:
    """Compare strategy performance vs buy-and-hold on a specific date range."""
    from datetime import datetime
    from rift.data import normalize_coin
    from rift.historical_data import load_candles_smart, load_funding_smart
    from rift.strategy import discover_strategies, get_strategy
    from rift.backtest import run_backtest
    import polars as pl
    import numpy as np

    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    coin = normalize_coin(pair)
    strategy_cls = get_strategy(strategy_name)
    strat = strategy_cls()
    if not interval:
        interval = strat.default_interval
    lev = strat.config.leverage if hasattr(strat.config, "leverage") else 1.0

    df = load_candles_smart(coin, interval)
    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No data for {coin} {interval}"})
        return

    if start:
        start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
        df = df.filter(pl.col("timestamp") >= start_ts)
    if end:
        end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)
        df = df.filter(pl.col("timestamp") <= end_ts)

    if len(df) < 10:
        _emit({"type": "error", "msg": f"Only {len(df)} candles in range — need at least 10"})
        return

    funding = load_funding_smart(coin)
    bt = run_backtest(strategy=strat, df=df, strategy_name=strategy_name, pair=coin,
                      interval=interval, initial_equity=10000, leverage=lev,
                      funding_df=funding, silent=True, use_fractional_sizing=True)

    closes = df["close"].to_numpy().astype(float)
    buy_hold_pct = round(((closes[-1] - closes[0]) / closes[0]) * 100, 2)
    alpha = round(bt.total_return_pct - buy_hold_pct, 2)

    _emit({
        "type": "result", "command": "verify",
        "strategy": strategy_name, "pair": coin,
        "period": {"from": start or "all", "to": end or "now", "candles": len(df)},
        "strategy_return_pct": round(bt.total_return_pct, 2),
        "buy_hold_return_pct": buy_hold_pct,
        "alpha_pct": alpha,
        "strategy_trades": bt.num_trades,
        "strategy_sharpe": round(bt.sharpe_ratio, 2),
        "strategy_max_dd": round(bt.max_drawdown_pct, 2),
        "verdict": "Strategy beat market" if alpha > 0 else "Market beat strategy",
    })


@app.command("research")
def research(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC-PERP", "--pair", help="Trading pair"),
    interval: str = typer.Option("", "--tf", help="Timeframe (auto-detected if empty)"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
    config_overrides_json: str = typer.Option("", "--config-overrides", help="JSON config overrides for optimization"),
) -> None:
    """Run full validation pipeline: backtest → walk-forward → Monte Carlo → multi-pair."""
    from rift.research import run_research_pipeline
    from rift.data import normalize_coin

    config_overrides = None
    if config_overrides_json:
        config_overrides = json.loads(config_overrides_json)

    result = run_research_pipeline(
        strategy_name=strategy_name,
        pair=normalize_coin(pair),
        interval=interval,
        initial_equity=equity,
        strategies_dir=strategies_dir,
        config_overrides=config_overrides,
    )

    if "error" in result and result.get("grade") is None:
        _emit({"type": "error", "msg": result["error"]})
        sys.exit(1)

    _emit({"type": "result", "command": "research", **result})
    grade = result.get("grade", "")
    # Auto-record lesson
    _record_lesson(
        coin=normalize_coin(pair), strategy=strategy_name,
        approach=f"research_{strategy_name}",
        result="pass" if grade in ("A", "B") else "fail",
        reason=result.get("verdict", ""),
        metrics={"grade": grade,
                 "sharpe": result.get("backtest", {}).get("sharpe", 0),
                 "return_pct": result.get("backtest", {}).get("return_pct", 0)},
    )
    if grade in ("A", "B"):
        _hint(f"Next: go live with 'rift algo {strategy_name} --pair {pair}'")
    elif grade == "C":
        _hint(f"Next: optimize with 'rift smart-sweep {strategy_name} --pair {pair}'")
    elif grade in ("D", "F"):
        _hint("Consider 'rift workbench-create' for a fresh approach.")


def _scout_watch_loop(
    interval_minutes: int,
    top_n: int, bias_tf: str, entry_tf: str,
    min_confluence: int, soak_seconds: int,
) -> None:
    """Run Scout on a timer, alert on high-confidence opportunities."""

    import dataclasses
    from rift.scout import scan_market

    # Load webhook config if available
    webhook_url = ""
    config_file = Path.home() / ".rift" / "config.json"
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text())
            webhook_url = cfg.get("alerts", {}).get("webhook_url", "")
        except Exception:
            pass

    _emit({"type": "status", "msg": f"Scout watch mode — scanning every {interval_minutes}min"})
    if webhook_url:
        _emit({"type": "status", "msg": f"Webhook alerts enabled: {webhook_url[:40]}..."})

    seen_keys: set[str] = set()  # prevent duplicate alerts within session

    while True:
        try:
            opportunities = scan_market(
                top_n=top_n, bias_tf=bias_tf, entry_tf=entry_tf,
                min_confluence=min_confluence, soak_seconds=soak_seconds,
            )

            for opp in opportunities:
                if opp.score >= 0.4 and opp.num_categories >= 4:
                    key = f"{opp.coin}:{opp.direction}:{int(time.time()) // 3600}"
                    if key not in seen_keys:
                        seen_keys.add(key)

                        alert_data = {
                            "coin": opp.coin,
                            "direction": opp.direction,
                            "score": opp.score,
                            "confidence": opp.confidence_tier,
                            "categories": opp.num_categories,
                            "leverage": opp.leverage,
                            "entry_price": opp.entry_price,
                            "target_price": opp.target_price,
                            "stop_price": opp.stop_price,
                            "hold_type": opp.hold_type,
                        }

                        _emit({"type": "alert", "event": "scout_opportunity", **alert_data})

                        # Fire webhook if configured
                        if webhook_url:
                            try:
                                from rift.alerts import fire_alert
                                fire_alert("scout_opportunity", alert_data, [{
                                    "type": "webhook",
                                    "url": webhook_url,
                                    "events": ["scout_opportunity"],
                                }])
                            except Exception:
                                pass

            _emit({"type": "status", "msg": f"Scan done — {len(opportunities)} opportunities. Next in {interval_minutes}min"})

        except Exception as e:
            _emit({"type": "error", "msg": f"Scout watch error: {e}"})

        time.sleep(interval_minutes * 60)


@app.command("scout")
def scout_cmd(
    top: int = typer.Option(20, "--top", help="Number of coins to scan"),
    bias_tf: str = typer.Option("1h", "--bias-tf", help="Higher timeframe for directional bias"),
    entry_tf: str = typer.Option("5m", "--entry-tf", help="Lower timeframe for entry timing"),
    min_confluence: int = typer.Option(2, "--min", help="Minimum signals on bias timeframe"),
    soak: int = typer.Option(120, "--soak", help="Seconds to collect live websocket data (0 = skip)"),
    no_soak: bool = typer.Option(False, "--no-soak", help="Skip websocket soak (faster, less accurate)"),
    watch: int = typer.Option(0, "--watch", help="Re-scan every N minutes and alert on high-confidence opportunities"),
    # Legacy compat
    tf: str = typer.Option("", "--tf", help="Legacy: sets bias timeframe", hidden=True),
) -> None:
    """Scan the market for opportunities using multi-timeframe bias + entry detection."""
    from rift.scout import scan_market
    import dataclasses

    if tf:
        bias_tf = tf
    soak_seconds = 0 if no_soak else soak

    if watch > 0:
        _scout_watch_loop(watch, top, bias_tf, entry_tf, min_confluence, soak_seconds)
        return

    opportunities = scan_market(
        top_n=top, bias_tf=bias_tf, entry_tf=entry_tf,
        min_confluence=min_confluence, soak_seconds=soak_seconds,
    )
    _emit({
        "type": "result", "command": "scout",
        "opportunities": [dataclasses.asdict(o) for o in opportunities],
        "scanned": top,
        "bias_tf": bias_tf,
        "entry_tf": entry_tf,
        "soak_seconds": soak_seconds,
    })


@app.command("signal-stats")
def signal_stats_cmd() -> None:
    """Show signal hit rate statistics from trade memory."""
    from rift.signal_memory import get_signal_stats, get_memory_size

    stats = get_signal_stats()
    _emit({
        "type": "result", "command": "signal-stats",
        "signals": stats,
        "total_observations": get_memory_size(),
    })


@app.command("signal-decay")
def signal_decay_cmd(
    signal_name: str = typer.Argument("", help="Signal name (empty = all signals)"),
) -> None:
    """Show signal timing decay stats — how fast signals move after firing."""
    from rift.signal_memory import get_signal_decay, get_signal_stats

    if signal_name:
        result = get_signal_decay(signal_name)
        _emit({"type": "result", "command": "signal-decay", "signal": signal_name, "decay": result})
    else:
        stats = get_signal_stats()
        decay_results = {}
        seen = set()
        for sig_key in stats:
            sig_name = sig_key.split(":")[0]
            if sig_name in seen:
                continue
            seen.add(sig_name)
            d = get_signal_decay(sig_name)
            if d:
                decay_results[sig_name] = d
        _emit({"type": "result", "command": "signal-decay", "signals": decay_results})


@app.command("signal-backfill")
def signal_backfill_cmd(
    top: int = typer.Option(10, "--top", help="Number of coins to backfill"),
    tf: str = typer.Option("1h", "--tf", help="Timeframe"),
    hold_candles: int = typer.Option(12, "--hold", help="Candles to hold for outcome check"),
    step: int = typer.Option(6, "--step", help="Step size between samples (skip N candles)"),
) -> None:
    """Backfill signal memory using the signal factory on historical data.

    Replays candle history for each coin, builds state dicts, runs all 38 signals
    via aggregate_signals(), checks the outcome N candles later, and records wins/losses
    to signal memory. This teaches Scout which signal combinations actually work.
    """
    import polars as pl
    from pathlib import Path
    from rift.scout import _compute_rsi, _ema, _bollinger, _keltner, _compute_atr
    from rift.signals.aggregator import aggregate_signals
    from rift.signal_memory import record_outcome, get_memory_size

    data_dir = Path(__file__).parent.parent.parent.parent.parent / "packages" / "cli" / "data"
    if not data_dir.exists():
        _emit({"type": "error", "msg": f"Data dir not found: {data_dir}"})
        return

    coins = sorted([d.name for d in data_dir.iterdir() if d.is_dir()])[:top]

    # Clear old memory if it exists (old format is incompatible)
    memory_file = Path.home() / ".rift" / "signal_memory.jsonl"
    if memory_file.exists():
        old_size = get_memory_size()
        if old_size > 0:
            _emit({"type": "status", "msg": f"Clearing {old_size} old signal memory entries (incompatible format)"})
            memory_file.write_text("")

    total_recorded = 0
    total_wins = 0
    total_losses = 0

    for coin_idx, coin in enumerate(coins):
        candle_file = data_dir / coin / f"candles_{tf}.parquet"
        if not candle_file.exists():
            continue

        df = pl.read_parquet(candle_file)
        closes = df["close"].to_list()
        highs = df["high"].to_list()
        lows = df["low"].to_list()
        volumes = df["volume"].to_list()

        if len(closes) < 100:
            continue

        # Load funding data if available (for funding signals)
        funding_rates = {}
        funding_file = data_dir / coin / "funding_hourly.parquet"
        if funding_file.exists():
            try:
                fdf = pl.read_parquet(funding_file)
                for row in fdf.iter_rows(named=True):
                    funding_rates[row["timestamp"]] = row.get("funding_rate", 0)
            except Exception:
                pass

        # Load OI data if available
        oi_data = {}
        oi_file = data_dir / coin / "oi_1h.parquet"
        if oi_file.exists():
            try:
                oidf = pl.read_parquet(oi_file)
                for row in oidf.iter_rows(named=True):
                    oi_data[row["timestamp"]] = row.get("oi_close", 0)
            except Exception:
                pass

        # Get candle timestamps for funding/OI lookup
        timestamps = df["timestamp"].to_list() if "timestamp" in df.columns else [0] * len(closes)

        coin_recorded = 0
        coin_wins = 0

        _emit({"type": "progress", "coin": coin, "pct": round((coin_idx + 1) / len(coins) * 100), "candles": len(closes)})

        # Slide through history with step size to avoid over-sampling
        for i in range(50, len(closes) - hold_candles, step):
            # Build price/volume windows
            price_history = closes[max(0, i - 20):i + 1]
            volume_history = volumes[max(0, i - 20):i + 1]
            c = closes[:i + 1]
            h = highs[:i + 1]
            l = lows[:i + 1]
            v = volumes[:i + 1]

            # Compute indicators
            rsi = _compute_rsi(c, 14)
            ema_fast = _ema(c, 20)
            ema_slow = _ema(c, 50)
            atr = _compute_atr(h, l, c, 14)
            bb_upper, bb_lower, bb_mid = _bollinger(c, 20, 2.0)
            kc_upper, kc_lower = _keltner(c, h, l, 20, 1.5)
            avg_vol = sum(v[-20:]) / min(20, len(v[-20:])) if v[-20:] else 1.0
            rel_vol = float(v[-1] / avg_vol) if avg_vol > 0 else 1.0

            # Approximate CVD from candle direction
            recent_cvd = 0.0
            recent_vol_delta = 0.0
            for j in range(max(0, i - 9), i + 1):
                o_val, c_val, v_val = float(df["open"][j]), closes[j], volumes[j]
                delta = v_val if c_val > o_val else -v_val if c_val < o_val else 0
                recent_cvd += delta
                recent_vol_delta = delta

            # Funding rate at this timestamp
            ts = timestamps[i] if i < len(timestamps) else 0
            funding_rate = funding_rates.get(ts, 0)
            # Try nearest hour if exact match fails
            if funding_rate == 0 and ts > 0:
                hour_ts = (ts // 3600000) * 3600000
                funding_rate = funding_rates.get(hour_ts, 0)

            # OI at this timestamp
            oi = oi_data.get(ts, 0)
            if oi == 0 and ts > 0:
                hour_ts = (ts // 3600000) * 3600000
                oi = oi_data.get(hour_ts, 0)

            # OI rate of change
            prev_oi = 0
            if ts > 0:
                prev_ts = (ts // 3600000 - 6) * 3600000  # 6 hours prior
                prev_oi = oi_data.get(prev_ts, 0)
            oi_roc = ((oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0

            current_price = closes[i]

            # Build state dict (same structure as Scout)
            state = {
                "price": current_price,
                "close": current_price,
                "price_history": price_history,
                "volume_history": volume_history,
                "indicators": {
                    "rsi": rsi,
                    "rsi_14": rsi,
                    "ema_fast": ema_fast,
                    "ema_slow": ema_slow,
                    "bb_upper": bb_upper,
                    "bb_lower": bb_lower,
                    "bb_mid": bb_mid,
                    "kc_upper": kc_upper,
                    "kc_lower": kc_lower,
                    "atr": float(atr) if atr > 0 else current_price * 0.01,
                },
                "funding_rate": funding_rate,
                "predicted_funding": 0,
                "premium": 0,
                "open_interest": oi,
                "oracle_price": current_price,
                "day_volume": 0,
                "oi_roc": oi_roc,
                "oi_delta": oi - prev_oi if prev_oi > 0 else 0,
                "oi_zscore": 0,
                "relative_volume": rel_vol,
                "cvd": recent_cvd,
                "volume_delta": recent_vol_delta,
                "funding_divergence": 0,
                "market_breadth_ob": 0,
                "market_breadth_os": 0,
                "market_avg_rsi": 50,
                "btc_momentum": 0,
                "net_delta": 0,
                "bids_depth": 0,
                "asks_depth": 0,
            }

            # Run all signals through the aggregator
            opp = aggregate_signals(coin, state)
            if opp is None or opp.num_signals < 2:
                continue

            # Check outcome: what happened N candles later?
            future_price = closes[i + hold_candles]
            if opp.direction == "LONG":
                pnl_pct = (future_price - current_price) / current_price * 100
            else:
                pnl_pct = (current_price - future_price) / current_price * 100

            signal_names = [s["name"] for s in opp.signals]
            record_outcome(coin, opp.direction.lower(), signal_names, pnl_pct, source="backfill")

            coin_recorded += 1
            total_recorded += 1
            if pnl_pct > 0:
                coin_wins += 1
                total_wins += 1
            else:
                total_losses += 1

        if coin_recorded > 0:
            coin_wr = coin_wins / coin_recorded * 100
            _emit({"type": "status", "msg": f"  {coin}: {coin_recorded} samples, {coin_wr:.1f}% win rate"})

    overall_wr = total_wins / total_recorded * 100 if total_recorded > 0 else 0
    _emit({
        "type": "result", "command": "signal-backfill",
        "total_recorded": total_recorded,
        "wins": total_wins,
        "losses": total_losses,
        "win_rate": round(overall_wr, 1),
        "memory_size": get_memory_size(),
        "coins": coins,
        "hold_candles": hold_candles,
        "step": step,
    })


@app.command("audit")
def audit_cmd(
    export: str = typer.Option("csv", "--export", help="Export format: csv or json"),
    last: int = typer.Option(30, "--last", help="Days of history to include"),
    strategy: str = typer.Option("", "--strategy", help="Filter by strategy name"),
    output: str = typer.Option("", "--output", help="Custom output path"),
) -> None:
    """Export compliance-grade audit trail of all live algo sessions.

    Reads `~/.rift/algo_sessions/ALGO_*.json` files written by `rift algo`
    sessions. Manual trades (`rift trade`) are NOT captured here — they
    emit per-event audit records to stdout (the `audit_record` NDJSON line
    that the CLI surface logs as the trade executes), but don't write a
    consolidated session file.
    """
    from pathlib import Path
    from rift.audit import export_audit_trail

    sessions_dir = Path.home() / ".rift" / "algo_sessions"
    if not sessions_dir.exists() or not any(sessions_dir.glob("ALGO_*.json")):
        _emit({
            "type": "result", "command": "audit",
            "path": "", "format": export,
            "exported_rows": 0,
            "note": (
                f"No algo sessions found in {sessions_dir}. Audit export covers "
                f"`rift algo` sessions only — manual trades emit per-event "
                f"audit_record lines to stdout but aren't bundled here."
            ),
        })
        return

    path = export_audit_trail(
        output_format=export,
        last_days=last,
        strategy=strategy,
        output_path=output,
    )
    _emit({"type": "result", "command": "audit", "path": path, "format": export})


@app.command("versions")
def versions_cmd(
    strategy_name: str = typer.Option("", "--strategy", help="Filter by strategy name"),
    diff: bool = typer.Option(False, "--diff", help="Show changes between last two versions"),
) -> None:
    """Show strategy version history."""
    from rift.versioning import get_version_history, diff_versions
    versions = get_version_history(strategy_name=strategy_name)
    result: dict = {"versions": versions}
    if diff and len(versions) >= 2:
        changes = diff_versions(versions[-2]["version_hash"], versions[-1]["version_hash"])
        result["diff"] = changes
    _emit({"type": "result", "command": "versions", **result})


