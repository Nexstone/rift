"""Data ingestion / catalog commands — extracted from cli.py in Phase 6.

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


@app.command("sync")
def sync_data(
    coins: str = typer.Option("", "--coins", help="Comma-separated coins (empty = auto-detect from strategies)"),
    timeframes: str = typer.Option("5m,15m,1h,4h", "--tf", help="Comma-separated timeframes to build"),
    start: str = typer.Option("2023-09-01", "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option("", "--end", help="End date (default: today)"),
    no_funding: bool = typer.Option(False, "--no-funding", help="Skip funding rate sync"),
    full: bool = typer.Option(False, "--full", help="Full sync (ignore incremental cache)"),
    aws_key: str = typer.Option("", "--aws-key", help="AWS Access Key ID (for non-interactive setup)"),
    aws_secret: str = typer.Option("", "--aws-secret", help="AWS Secret Access Key"),
) -> None:
    """Sync historical data from Hyperliquid S3. Requires AWS credentials (free tier)."""
    from rift.s3_data import check_aws_credentials, sync_coins
    from rift.config import set_env_var

    # Handle inline AWS credential setup
    if aws_key and aws_secret:
        set_env_var("AWS_ACCESS_KEY_ID", aws_key)
        set_env_var("AWS_SECRET_ACCESS_KEY", aws_secret)
        set_env_var("AWS_DEFAULT_REGION", "ap-northeast-1")
        _emit({"type": "status", "msg": "AWS credentials saved to ~/.rift/.env"})

    if not check_aws_credentials():
        _emit({"type": "status", "msg": "No AWS credentials found."})
        _emit({"type": "status", "msg": ""})
        _emit({"type": "status", "msg": "RIFT uses Hyperliquid's S3 data archive for historical data."})
        _emit({"type": "status", "msg": "You need a free AWS account (costs ~$2 for full data download)."})
        _emit({"type": "status", "msg": ""})
        _emit({"type": "status", "msg": "1. Create account at aws.amazon.com"})
        _emit({"type": "status", "msg": "2. IAM > Create User > Attach AmazonS3ReadOnlyAccess"})
        _emit({"type": "status", "msg": "3. Create Access Key > paste below"})

        try:
            import sys as _sys
            print("", file=_sys.stderr)
            access_key = input("  AWS Access Key ID: ").strip()
            secret_key = input("  AWS Secret Access Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            _emit({"type": "error", "msg": "Setup cancelled. Run: rift sync --aws-key AKIA... --aws-secret ..."})
            return

        if not access_key or not secret_key:
            _emit({"type": "error", "msg": "Both keys required."})
            return

        set_env_var("AWS_ACCESS_KEY_ID", access_key)
        set_env_var("AWS_SECRET_ACCESS_KEY", secret_key)
        set_env_var("AWS_DEFAULT_REGION", "ap-northeast-1")
        _emit({"type": "status", "msg": "Credentials saved to ~/.rift/.env"})

    # Determine coins to sync
    if coins:
        from rift.data import normalize_coin
        coin_list = [normalize_coin(c.strip()) for c in coins.split(",")]
    else:
        # Auto-detect from registered strategies
        from rift.strategy import discover_strategies as _ds, list_strategies as _ls
        dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies", Path(__file__).parent / "strategies"]
        from rift.workbench import GENERATED_DIR
        if GENERATED_DIR.exists():
            dirs.append(GENERATED_DIR)
        _ds(dirs)

        coin_list = []
        for name, cls in _ls().items():
            module = sys.modules.get(cls.__module__)
            if module and hasattr(module, "COIN_CONFIGS"):
                coin_list.extend(getattr(module, "COIN_CONFIGS").keys())

        if not coin_list:
            # Default set if no strategies registered
            coin_list = ["BTC", "ETH", "SOL", "SUI", "AVAX", "ZEC", "NEAR"]

        coin_list = list(set(coin_list))

    tf_list = [t.strip() for t in timeframes.split(",")]
    end_date = end or time.strftime("%Y-%m-%d")

    coin_list_sorted = sorted(coin_list)
    _emit({"type": "progress", "pct": 0,
           "msg": f"Syncing {len(coin_list_sorted)} coins from Hyperliquid S3 ({start} → {end_date}) — bulk pass, shared download/parse"})

    def on_progress(msg):
        _emit({"type": "progress", "pct": 0, "msg": msg})

    results = []
    try:
        stats_by_coin = sync_coins(
            coins=coin_list_sorted,
            timeframes=tf_list,
            start_date=start,
            end_date=end_date,
            include_funding=not no_funding,
            incremental=not full,
            on_progress=on_progress,
        )
        for coin, stats in stats_by_coin.items():
            results.append({"coin": coin, "status": "ok", **stats})
            candle_summary = ", ".join(f"{tf}: {n}" for tf, n in stats.get("candles", {}).items())
            _emit({"type": "progress", "pct": 100,
                   "msg": f"{coin}: {candle_summary}, {stats.get('funding', 0)} funding entries, {stats.get('fills', 0):,} fills"})
    except Exception as e:
        _emit({"type": "error", "msg": f"sync failed: {e}"})
        for coin in coin_list_sorted:
            results.append({"coin": coin, "status": "error", "error": str(e)})

    _emit({
        "type": "result", "command": "sync",
        "coins_synced": len([r for r in results if r["status"] == "ok"]),
        "coins_failed": len([r for r in results if r["status"] == "error"]),
        "results": results,
    })
    _hint("Data synced. Run 'rift backtest <strategy> --pair <COIN>' to test.")


@app.command("fetch")
def fetch_data(
    pair: str = typer.Argument(..., help="Trading pair (e.g. BTC, ETH-PERP)"),
    interval: str = typer.Option("15m", "--tf", "--interval", help="Candle interval"),
    start: str = typer.Option("", "--start", help="Start date YYYY-MM-DD (optional)"),
) -> None:
    """Fetch and cache candle data from Hyperliquid."""

    from rift.data import fetch_candles, save_candles, fetch_funding_rates, save_funding_rates

    start_time = None
    if start:
        from datetime import datetime
        dt = datetime.strptime(start, "%Y-%m-%d")
        start_time = int(dt.timestamp() * 1000)

    _emit({"type": "progress", "pct": 0, "msg": f"Fetching {pair} {interval} candles..."})

    df = fetch_candles(pair, interval, start_time=start_time)
    _emit({"type": "progress", "pct": 60, "msg": f"Fetched {len(df)} candles, fetching funding rates..."})

    # Also fetch funding rates
    funding_count = 0
    try:
        candle_start = int(df["timestamp"].min()) if len(df) > 0 else start_time
        funding_df = fetch_funding_rates(pair, start_time=candle_start)
        if len(funding_df) > 0:
            save_funding_rates(funding_df, pair)
            funding_count = len(funding_df)
    except Exception:
        pass  # Funding rates are optional, don't fail the whole fetch

    _emit({"type": "progress", "pct": 90, "msg": f"Fetched {len(df)} candles + {funding_count} funding rates, saving..."})

    path = save_candles(df, pair, interval)
    _emit({"type": "progress", "pct": 100, "msg": "Done"})
    _emit({
        "type": "result",
        "command": "fetch",
        "pair": pair,
        "interval": interval,
        "candles": len(df),
        "path": str(path),
        "start": str(df["timestamp"].min()) if len(df) > 0 else None,
        "end": str(df["timestamp"].max()) if len(df) > 0 else None,
    })


@app.command("list-pairs")
def list_pairs(
    top: int = typer.Option(20, "--top", help="Number of top pairs by volume"),
) -> None:
    """List available trading pairs from Hyperliquid."""
    from rift.data import get_info_client

    info = get_info_client()
    meta = info.meta()
    asset_ctxs = info.meta_and_asset_ctxs()

    # asset_ctxs is [meta, [ctx1, ctx2, ...]]
    ctxs = asset_ctxs[1] if isinstance(asset_ctxs, list) and len(asset_ctxs) > 1 else []

    pairs = []
    for i, asset in enumerate(meta["universe"]):
        name = asset["name"]
        volume_24h = 0
        if i < len(ctxs):
            volume_24h = float(ctxs[i].get("dayNtlVlm", 0))
        pairs.append({"name": name, "volume_24h": round(volume_24h)})

    # Sort by volume descending
    pairs.sort(key=lambda x: x["volume_24h"], reverse=True)
    pairs = pairs[:top]

    _emit({"type": "result", "command": "list-pairs", "pairs": pairs})


@app.command("fetch-multi")
def fetch_multi(
    pairs_csv: str = typer.Argument(..., help="Comma-separated pairs or 'top20'"),
    interval: str = typer.Option("1h", "--tf", "--interval", help="Candle interval"),
    start: str = typer.Option("", "--start", help="Start date YYYY-MM-DD (optional)"),
    top: int = typer.Option(20, "--top", help="Number of top pairs when using 'top'"),
) -> None:
    """Fetch candle data for multiple pairs."""
    from rift.data import fetch_candles, save_candles, get_info_client

    start_time = None
    if start:
        from datetime import datetime
        dt = datetime.strptime(start, "%Y-%m-%d")
        start_time = int(dt.timestamp() * 1000)

    # Resolve pair list
    if pairs_csv.startswith("top"):
        n = int(pairs_csv.replace("top", "") or str(top))
        info = get_info_client()
        meta = info.meta()
        asset_ctxs = info.meta_and_asset_ctxs()
        ctxs = asset_ctxs[1] if isinstance(asset_ctxs, list) and len(asset_ctxs) > 1 else []

        volume_pairs = []
        for i, asset in enumerate(meta["universe"]):
            vol = float(ctxs[i].get("dayNtlVlm", 0)) if i < len(ctxs) else 0
            volume_pairs.append((asset["name"], vol))
        volume_pairs.sort(key=lambda x: x[1], reverse=True)
        pair_list = [p[0] for p in volume_pairs[:n]]
    else:
        from rift.data import normalize_coin as _nc
        pair_list = [_nc(p.strip()) for p in pairs_csv.split(",")]

    results = []
    total = len(pair_list)

    for i, pair in enumerate(pair_list):
        pct = int(i / total * 100)
        _emit({"type": "progress", "pct": pct, "msg": f"Fetching {pair} ({i+1}/{total})..."})

        try:
            df = fetch_candles(pair, interval, start_time=start_time)
            path = save_candles(df, pair, interval)
            results.append({"pair": pair, "candles": len(df), "status": "ok"})
        except Exception as e:
            results.append({"pair": pair, "candles": 0, "status": "fail", "error": str(e)})

    _emit({"type": "result", "command": "fetch-multi", "results": results, "total": len(results)})


@app.command("list-data")
def list_data() -> None:
    """List cached candle data."""
    from rift.data import list_cached_data

    cached = list_cached_data()
    _emit({"type": "result", "command": "list-data", "data": cached})


@app.command("data-inventory")
def data_inventory() -> None:
    """Show all available data: coins, timeframes, candle counts, date ranges."""
    from datetime import datetime
    from rift.historical_data import load_candles_smart, load_funding_smart

    # Scan bundled + cached data for known coins and timeframes
    # Also scan ~/.rift/data/ for any additional coins the user has collected
    from rift.data import DEFAULT_DATA_DIR, path_to_coin
    coins_to_check = [
        "BTC", "ETH", "SOL", "XRP", "AVAX", "SUI", "NEAR", "WIF", "ZEC",
        "ONDO", "DOGE", "LINK", "ADA", "DOT", "MATIC",
        # TradFi (HIP-3)
        "xyz:SP500", "xyz:XYZ100", "xyz:CL", "xyz:SILVER", "xyz:GOLD",
        "xyz:TSLA", "xyz:NVDA", "xyz:AAPL", "xyz:BRENTOIL", "xyz:COPPER",
    ]
    # Also include any coins found in the cache directory
    if DEFAULT_DATA_DIR.exists():
        for d in DEFAULT_DATA_DIR.iterdir():
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
                cached_coin = path_to_coin(d.name)
                if cached_coin not in coins_to_check:
                    coins_to_check.append(cached_coin)
    timeframes = ["5m", "15m", "1h", "4h"]

    coins: dict[str, dict] = {}
    total_candles = 0

    for coin in coins_to_check:
        coin_data = {}
        for tf in timeframes:
            try:
                df = load_candles_smart(coin, tf)
                if df is not None and len(df) > 0:
                    ts = df["timestamp"].to_numpy()
                    start = datetime.fromtimestamp(int(ts[0]) / 1000).strftime("%Y-%m-%d")
                    end = datetime.fromtimestamp(int(ts[-1]) / 1000).strftime("%Y-%m-%d")
                    coin_data[tf] = {"candles": len(df), "start": start, "end": end}
                    total_candles += len(df)
            except Exception:
                pass
        if coin_data:
            coins[coin] = coin_data

    # Scan funding data
    funding_data: dict[str, dict] = {}
    for coin in coins:
        try:
            fdf = load_funding_smart(coin)
            if fdf is not None and len(fdf) > 0:
                ts = fdf["timestamp"].to_numpy()
                start = datetime.fromtimestamp(int(ts[0]) / 1000).strftime("%Y-%m-%d")
                end = datetime.fromtimestamp(int(ts[-1]) / 1000).strftime("%Y-%m-%d")
                funding_data[coin] = {"rows": len(fdf), "start": start, "end": end}
        except Exception:
            pass

    _emit({
        "type": "result",
        "command": "data-inventory",
        "coins": coins,
        "total_coins": len(coins),
        "total_candles": total_candles,
        "funding_data": funding_data,
    })


@app.command("diff")
def experiment_diff(
    exp_a: int = typer.Argument(..., help="First experiment ID"),
    exp_b: int = typer.Argument(..., help="Second experiment ID"),
) -> None:
    """Compare two experiments — show config changes and metric deltas."""
    from rift.workbench import get_experiment_by_id
    import json as _json

    a = get_experiment_by_id(exp_a)
    b = get_experiment_by_id(exp_b)
    if a is None:
        _emit({"type": "error", "msg": f"Experiment {exp_a} not found."})
        return
    if b is None:
        _emit({"type": "error", "msg": f"Experiment {exp_b} not found."})
        return

    # Config diff
    config_a = _json.loads(a.get("config_json", "{}"))
    config_b = _json.loads(b.get("config_json", "{}"))
    config_changes = {}
    all_keys = set(list(config_a.keys()) + list(config_b.keys()))
    for k in sorted(all_keys):
        va = config_a.get(k)
        vb = config_b.get(k)
        if va != vb:
            config_changes[k] = {"from": va, "to": vb}

    # Metric deltas
    metric_keys = ["return_pct", "sharpe", "num_trades", "win_rate", "max_drawdown", "profit_factor"]
    metric_deltas = {}
    for k in metric_keys:
        va = a.get(k, 0) or 0
        vb = b.get(k, 0) or 0
        metric_deltas[k] = {"from": round(va, 4), "to": round(vb, 4), "delta": round(vb - va, 4)}

    _emit({
        "type": "result",
        "command": "diff",
        "exp_a": exp_a,
        "exp_b": exp_b,
        "strategy": a.get("strategy_name", ""),
        "config_changes": config_changes,
        "metric_deltas": metric_deltas,
    })


@app.command("collect")
def collect(
    symbols: str = typer.Option("BTC,ETH,SOL,HYPE", "--symbols", help="Comma-separated symbols"),
    timeframes: str = typer.Option("1m,5m,15m,1h,4h,1d", "--tf", help="Comma-separated timeframes (default covers minute to daily)"),
    orderbook_interval: int = typer.Option(60, "--ob-interval", help="Seconds between orderbook snapshots"),
) -> None:
    """Start the persistent data collector."""
    from rift.collector import run_collector

    symbol_list = [s.strip().upper() for s in symbols.split(",")]
    tf_list = [t.strip() for t in timeframes.split(",")]

    run_collector(
        symbols=symbol_list,
        timeframes=tf_list,
        orderbook_interval=orderbook_interval,
    )


@app.command("collect-status")
def collect_status() -> None:
    """Get collector status and statistics."""
    from rift.collector import get_collector_stats

    stats = get_collector_stats()
    _emit({"type": "result", "command": "collect-status", **stats})


