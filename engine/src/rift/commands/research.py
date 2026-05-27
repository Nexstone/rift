"""Backtest / walk-forward / Monte Carlo / sweep commands — extracted from cli.py in Phase 6.

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
from rift.commands.research_tools import _record_lesson


@app.command("backtest")
def backtest(
    strategy_name: str = typer.Argument(..., help="Strategy name (registered via @register)"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("", "--tf", "--interval", help="Candle interval (auto-detected from strategy if empty)"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity in USDC"),
    leverage: float = typer.Option(1.0, "--leverage", help="Leverage multiplier"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
    all_pairs: bool = typer.Option(False, "--all-pairs", help="Run across top pairs and rank results"),
    top: int = typer.Option(10, "--top", help="Number of top pairs when using --all-pairs"),
) -> None:
    """Run a backtest on cached candle data."""
    from rift.data import load_candles, load_funding_rates, fetch_candles, save_candles, fetch_funding_rates, save_funding_rates
    from rift.backtest import run_backtest
    from rift.strategy import discover_strategies, get_strategy

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    # Load strategy
    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)

    # Use strategy's default interval if not specified
    if not interval:
        interval = strategy_cls.default_interval
        _emit({"type": "progress", "pct": 0, "msg": f"Using {strategy_name}'s default timeframe: {interval}"})

    # Multi-pair mode
    if all_pairs:
        _run_all_pairs_backtest(strategy_cls, strategy_name, interval, equity, leverage, top, strategies_dir)
        return

    strategy = strategy_cls()

    # Auto-fetch data if not cached
    df = load_candles(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "progress", "pct": 0, "msg": f"No cached data for {pair} {interval}. Fetching from Hyperliquid..."})
        try:
            df = fetch_candles(pair, interval)
            if len(df) > 0:
                save_candles(df, pair, interval)
                _emit({"type": "progress", "pct": 10, "msg": f"Fetched {len(df)} candles"})
            else:
                _emit({"type": "error", "msg": f"Could not fetch data for {pair} {interval}."})
                sys.exit(1)
        except Exception as e:
            _emit({"type": "error", "msg": f"Failed to fetch data for {pair}: {e}"})
            sys.exit(1)

    # Auto-fetch funding rates if not cached
    funding_df = load_funding_rates(pair)
    if funding_df is None or len(funding_df) == 0:
        try:
            candle_start = int(df["timestamp"].min())
            funding_df = fetch_funding_rates(pair, start_time=candle_start)
            if len(funding_df) > 0:
                save_funding_rates(funding_df, pair)
        except Exception:
            pass  # Funding is optional

    funding_msg = f", {len(funding_df)} funding rates" if funding_df is not None and len(funding_df) > 0 else ""

    _emit({"type": "progress", "pct": 0, "msg": f"Running backtest: {strategy_name} on {pair} {interval} ({len(df)} candles{funding_msg})"})

    result = run_backtest(
        strategy=strategy,
        df=df,
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        initial_equity=equity,
        leverage=leverage,
        funding_df=funding_df,
    )

    from rift.output import format_result_full
    _emit(format_result_full(result))
    _hint(f"Next: validate robustness with 'rift walk-forward {strategy_name} --pair {pair}'")


def _run_single_pair_backtest(args):
    """Worker function for parallel backtesting. Runs in a separate process."""
    import os
    # Limit BLAS threads per worker to prevent thread contention with multiprocessing
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

    strategy_cls, strategy_name, pair, interval, equity, leverage, df_path, funding_path = args
    import polars as pl
    from rift.backtest import run_backtest

    try:
        df = pl.read_parquet(df_path)
        funding_df = None
        if funding_path:
            try:
                funding_df = pl.read_parquet(funding_path)
            except Exception:
                pass

        strategy = strategy_cls()
        result = run_backtest(
            strategy=strategy, df=df,
            strategy_name=strategy_name, pair=pair, interval=interval,
            initial_equity=equity, leverage=leverage,
            funding_df=funding_df, silent=True,
        )
        return {
            "pair": pair,
            "return_pct": round(result.total_return_pct, 2),
            "sharpe": round(result.sharpe_ratio, 4),
            "profit_factor": round(result.profit_factor, 2),
            "max_drawdown_pct": round(result.max_drawdown_pct, 2),
            "win_rate": round(result.win_rate, 2),
            "num_trades": result.num_trades,
            "total_funding": round(result.total_funding, 2),
        }
    except Exception:
        return None


def _run_all_pairs_backtest(strategy_cls, strategy_name, interval, equity, leverage, top, strategies_dir):
    """Run a strategy across top pairs and rank results. Uses parallel processing."""
    import multiprocessing
    from rift.data import load_candles, load_funding_rates, fetch_candles, save_candles, fetch_funding_rates, save_funding_rates, get_info_client

    # Get top pairs by volume
    _emit({"type": "progress", "pct": 0, "msg": f"Fetching top {top} pairs by volume..."})
    try:
        info = get_info_client()
        meta = info.meta()
        asset_ctxs = info.meta_and_asset_ctxs()
        ctxs = asset_ctxs[1] if isinstance(asset_ctxs, list) and len(asset_ctxs) > 1 else []

        volume_pairs = []
        for i, asset in enumerate(meta["universe"]):
            vol = float(ctxs[i].get("dayNtlVlm", 0)) if i < len(ctxs) else 0
            volume_pairs.append((asset["name"], vol))
        volume_pairs.sort(key=lambda x: x[1], reverse=True)
        pair_list = [p[0] for p in volume_pairs[:top]]
    except Exception as e:
        _emit({"type": "error", "msg": f"Could not fetch pair list: {e}"})
        sys.exit(1)

    # Phase 1: Fetch all data sequentially (API rate limits)
    pair_data = []  # (pair, df_path, funding_path)
    total = len(pair_list)

    for idx, pair in enumerate(pair_list):
        pct = int((idx / total) * 40)
        _emit({"type": "progress", "pct": pct, "msg": f"Loading data {pair} ({idx+1}/{total})..."})

        df = load_candles(pair, interval)
        if df is None or len(df) == 0:
            try:
                df = fetch_candles(pair, interval)
                if len(df) > 0:
                    save_candles(df, pair, interval)
            except Exception:
                continue

        if df is None or len(df) < 100:
            continue

        funding_df = load_funding_rates(pair)
        if funding_df is None or len(funding_df) == 0:
            try:
                funding_df = fetch_funding_rates(pair, start_time=int(df["timestamp"].min()))
                if len(funding_df) > 0:
                    save_funding_rates(funding_df, pair)
            except Exception:
                pass

        # Get file paths for the parquet data (workers read from disk)
        from pathlib import Path
        home = Path.home()
        df_path = str(home / f".rift/data/{pair}/{interval}/candles.parquet")
        funding_path = str(home / f".rift/data/{pair}/funding.parquet")
        if not Path(funding_path).exists():
            funding_path = ""

        pair_data.append((pair, df_path, funding_path))

    if not pair_data:
        _emit({"type": "error", "msg": "No data available for any pairs."})
        return

    # Phase 2: Run backtests in parallel
    _emit({"type": "progress", "pct": 50, "msg": f"Running {len(pair_data)} backtests in parallel..."})

    worker_args = [
        (strategy_cls, strategy_name, pair, interval, equity, leverage, df_path, funding_path)
        for pair, df_path, funding_path in pair_data
    ]

    # Detect ML-heavy strategies (HMM) and limit parallelism
    is_ml_strategy = hasattr(strategy_cls, 'config_class') and strategy_cls.config_class and hasattr(strategy_cls.config_class, 'n_states')
    if is_ml_strategy:
        # ML strategies: fewer workers to avoid BLAS thread contention
        n_workers = min(len(worker_args), max(1, multiprocessing.cpu_count() // 3))
        _emit({"type": "progress", "pct": 50, "msg": f"ML strategy detected — using {n_workers} workers..."})
    else:
        n_workers = min(len(worker_args), max(1, multiprocessing.cpu_count() - 2))

    try:
        with multiprocessing.Pool(processes=n_workers) as pool:
            raw_results = pool.map(_run_single_pair_backtest, worker_args)
    except Exception:
        # Fallback to sequential if multiprocessing fails
        _emit({"type": "progress", "pct": 50, "msg": "Parallel failed, running sequentially..."})
        raw_results = [_run_single_pair_backtest(a) for a in worker_args]

    results = [r for r in raw_results if r is not None]

    # Sort by Sharpe ratio
    results.sort(key=lambda r: r["sharpe"], reverse=True)

    _emit({"type": "result", "command": "backtest-all-pairs", "strategy": strategy_name, "interval": interval, "results": results})


@app.command("sweep")
def sweep(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", "--interval", help="Candle interval"),
    config_path: str = typer.Option("", "--config", help="Path to sweep.yaml config file"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity"),
    leverage: float = typer.Option(1.0, "--leverage", help="Leverage multiplier"),
    top: int = typer.Option(10, "--top", help="Number of top results to show"),
    rank_by: str = typer.Option("sharpe", "--rank", help="Rank by: sharpe, return, or profit_factor"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
) -> None:
    """Run a parameter sweep across all combinations."""
    import yaml
    from rift.data import load_candles, load_funding_rates
    from rift.strategy import discover_strategies, get_strategy
    from rift.sweep import run_sweep, parse_sweep_config

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)

    # Load data
    df = load_candles(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No cached data for {pair} {interval}. Run 'rift data fetch --pair {pair} --tf {interval}' first."})
        sys.exit(1)

    # Load sweep config
    if config_path:
        sweep_config = yaml.safe_load(Path(config_path).read_text())
        sweep_params = parse_sweep_config(sweep_config)
    elif strategy_cls.config_class:
        # Auto-generate reasonable ranges from defaults
        import dataclasses
        sweep_params = {}

        # Parameters that should NOT be auto-swept (ML internals, meta-params)
        SKIP_PARAMS = {
            "leverage",
            # HMM internals — these are model architecture, not trading params
            "n_states", "train_window", "retrain_interval", "n_restarts", "vol_window",
            # Meta params that don't affect trading logic
            "n_iter", "random_state", "tol",
        }

        for f in dataclasses.fields(strategy_cls.config_class):
            if f.name in SKIP_PARAMS:
                continue
            # Skip booleans (bool is a subclass of int in Python)
            if isinstance(f.default, bool):
                continue
            if isinstance(f.default, int) and not isinstance(f.default, bool):
                val = f.default
                if val > 0:
                    # Ensure at least 3 distinct values for meaningful sweep
                    step = max(1, val // 3)
                    low = max(1, val - step)
                    high = val + step
                    sweep_params[f.name] = sorted(set([low, val, high]))
            elif isinstance(f.default, float) and f.default > 0:
                val = f.default
                # Use enough decimal precision to avoid rounding to zero
                precision = max(6, len(str(val).rstrip('0').split('.')[-1]) if '.' in str(val) else 0)
                low = round(val * 0.5, precision)
                high = round(val * 1.5, precision)
                sweep_params[f.name] = sorted(set([low, val, high]))

        if not sweep_params:
            _emit({"type": "error", "msg": "No sweepable parameters found. Provide a --config sweep.yaml file."})
            sys.exit(1)

        # Calculate total combos and warn if too many
        import itertools
        total_combos = 1
        for vals in sweep_params.values():
            total_combos *= len(vals)

        MAX_AUTO_COMBOS = 2000
        if total_combos > MAX_AUTO_COMBOS:
            # Too many combos — only sweep the top 5 most impactful params
            # (those with the widest relative range)
            ranked = sorted(
                sweep_params.items(),
                key=lambda kv: max(kv[1]) / min(kv[1]) if min(kv[1]) > 0 else 0,
                reverse=True,
            )
            sweep_params = dict(ranked[:5])
            new_total = 1
            for vals in sweep_params.values():
                new_total *= len(vals)
            _emit({"type": "progress", "pct": 0, "msg": f"Too many combos ({total_combos:,}). Reduced to top 5 params ({new_total} combos)."})
    else:
        _emit({"type": "error", "msg": "Strategy has no config. Provide a --config sweep.yaml file."})
        sys.exit(1)

    def on_progress(pct: int, msg: str) -> None:
        _emit({"type": "progress", "pct": pct, "msg": msg})

    funding_df = load_funding_rates(pair)

    result = run_sweep(
        strategy_cls=strategy_cls,
        df=df,
        sweep_params=sweep_params,
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        initial_equity=equity,
        leverage=leverage,
        funding_df=funding_df,
        on_progress=on_progress,
    )

    # Get top results
    if rank_by == "return":
        top_entries = result.top_by_return(top)
    elif rank_by == "profit_factor":
        top_entries = result.top_by_profit_factor(top)
    else:
        top_entries = result.top_by_sharpe(top)

    _emit({
        "type": "result",
        "command": "sweep",
        **result.to_dict(),
        "rank_by": rank_by,
        "top": [e.to_dict() for e in top_entries],
    })


@app.command("montecarlo")
def montecarlo(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", "--interval", help="Candle interval"),
    runs: int = typer.Option(10000, "--runs", help="Number of simulations"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity"),
    leverage: float = typer.Option(1.0, "--leverage", help="Leverage multiplier"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
) -> None:
    """Run Monte Carlo simulation on a backtest's trade sequence."""
    from rift.data import load_candles, load_funding_rates
    from rift.backtest import run_backtest
    from rift.strategy import discover_strategies, get_strategy
    from rift.montecarlo import run_montecarlo

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)
    strategy = strategy_cls()

    df = load_candles(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No cached data for {pair} {interval}. Run 'rift data fetch --pair {pair} --tf {interval}' first."})
        sys.exit(1)

    # Load funding rates
    funding_df = load_funding_rates(pair)

    # First run a backtest to get trades
    _emit({"type": "progress", "pct": 0, "msg": "Running backtest to collect trades..."})
    bt_result = run_backtest(
        strategy=strategy, df=df, strategy_name=strategy_name,
        pair=pair, interval=interval, initial_equity=equity, leverage=leverage,
        silent=True, funding_df=funding_df,
    )

    if not bt_result.trades:
        _emit({"type": "error", "msg": "Backtest produced no trades. Cannot run Monte Carlo on zero trades."})
        sys.exit(1)

    def on_progress(pct: int, msg: str) -> None:
        _emit({"type": "progress", "pct": pct, "msg": msg})

    try:
        mc_result = run_montecarlo(bt_result, num_simulations=runs, on_progress=on_progress)
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    _emit({"type": "result", "command": "montecarlo", **mc_result.to_dict()})


@app.command("walk-forward")
def walk_forward(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", "--interval", help="Candle interval"),
    config: str = typer.Option("3m/1m", "--wf", help="Walk-forward config: train/test (e.g. 3m/1m)"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity per window"),
    leverage: float = typer.Option(1.0, "--leverage", help="Leverage multiplier"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
) -> None:
    """Run walk-forward analysis to test strategy robustness."""
    from rift.historical_data import load_candles_smart, load_funding_smart
    from rift.strategy import discover_strategies, get_strategy
    from rift.walkforward import run_walk_forward, parse_walk_forward_config

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    # Load strategy
    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)
    strategy = strategy_cls()

    # Parse config
    try:
        train_months, test_months = parse_walk_forward_config(config)
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    # Load data — use smart loader for access to full historical parquets
    df = load_candles_smart(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No cached data for {pair} {interval}. Run 'rift data fetch --pair {pair} --tf {interval}' first."})
        sys.exit(1)

    def on_progress(pct: int, msg: str) -> None:
        _emit({"type": "progress", "pct": pct, "msg": msg})

    funding_df = load_funding_smart(pair)

    try:
        result = run_walk_forward(
            strategy=strategy,
            df=df,
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            train_months=train_months,
            test_months=test_months,
            initial_equity=equity,
            leverage=leverage,
            on_progress=on_progress,
            funding_df=funding_df,
            strategy_cls=strategy_cls,
        )
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    _emit({"type": "result", "command": "walk-forward", **result.to_dict()})
    # Auto-record lesson
    _record_lesson(
        coin=pair, strategy=strategy_name,
        approach=f"walkforward_{strategy_name}",
        result="pass" if result.pct_profitable_windows >= 60 else "fail",
        reason=f"OOS {result.combined_oos_return:+.1f}%, {result.pct_profitable_windows:.0f}% windows profitable, MC p={result.mc_p_value}",
        metrics={"oos_return": result.combined_oos_return,
                 "pct_profitable": result.pct_profitable_windows,
                 "mc_p_value": result.mc_p_value},
    )
    _hint(f"Next: run full validation with 'rift research {strategy_name} --pair {pair}'")


@app.command("compare")
def compare(
    strategy_names: str = typer.Argument(..., help="Comma-separated strategy names"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("15m", "--tf", "--interval", help="Candle interval"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity in USDC"),
    leverage: float = typer.Option(1.0, "--leverage", help="Leverage multiplier"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
) -> None:
    """Compare multiple strategies head-to-head."""
    from rift.data import load_candles
    from rift.backtest import run_backtest
    from rift.strategy import discover_strategies, get_strategy

    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    df = load_candles(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No cached data for {pair} {interval}. Run 'rift data fetch' first."})
        sys.exit(1)

    names = [n.strip() for n in strategy_names.split(",")]
    results = []

    for i, name in enumerate(names):
        _emit({"type": "progress", "pct": int(i / len(names) * 100), "msg": f"Backtesting {name}..."})
        try:
            strategy_cls = get_strategy(name)
        except KeyError as e:
            _emit({"type": "error", "msg": str(e).strip('"')})
            sys.exit(1)
        strategy = strategy_cls()
        result = run_backtest(
            strategy=strategy, df=df, strategy_name=name,
            pair=pair, interval=interval, initial_equity=equity, leverage=leverage,
        )
        results.append(result.to_dict())

    _emit({"type": "result", "command": "compare", "results": results})


@app.command("validate-strategy")
def validate_strategy(
    strategy_path: str = typer.Argument(..., help="Path to strategy .py file"),
) -> None:
    """Validate that a strategy file loads and registers correctly."""
    from rift.strategy import _REGISTRY, load_strategy_file

    file_path = Path(strategy_path)
    if not file_path.exists():
        _emit({"type": "result", "command": "validate-strategy", "status": "fail", "error": f"File not found: {strategy_path}"})
        sys.exit(1)

    # Snapshot registry before loading
    before = set(_REGISTRY.keys())

    try:
        load_strategy_file(file_path)
    except Exception as e:
        _emit({"type": "result", "command": "validate-strategy", "status": "fail", "error": f"Import error: {e}"})
        sys.exit(1)

    # Check what was registered
    after = set(_REGISTRY.keys())
    new_strategies = after - before

    if not new_strategies:
        _emit({
            "type": "result",
            "command": "validate-strategy",
            "status": "warn",
            "error": "File loaded but no strategy was registered. Make sure you use @register('name') decorator.",
        })
        sys.exit(1)

    # Validate each new strategy
    for name in new_strategies:
        cls = _REGISTRY[name]
        errors = []

        # Check on_candle is implemented
        if cls.on_candle is None or cls.on_candle.__qualname__.startswith("Strategy."):
            errors.append("on_candle() not implemented")

        # Check indicators returns something
        try:
            instance = cls()
            indicators = instance.indicators()
        except Exception as e:
            errors.append(f"Failed to instantiate: {e}")
            indicators = {}

        if errors:
            _emit({
                "type": "result",
                "command": "validate-strategy",
                "status": "warn",
                "name": name,
                "errors": errors,
            })
        else:
            _emit({
                "type": "result",
                "command": "validate-strategy",
                "status": "ok",
                "name": name,
                "indicators": list(indicators.keys()),
            })


@app.command("smart-sweep")
def smart_sweep_cmd(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", help="Timeframe"),
    n_trials: int = typer.Option(80, "--trials", help="Number of optimization trials"),
    target: str = typer.Option("sharpe", "--target", help="Optimize for: sharpe, return, calmar"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategy directory"),
) -> None:
    """Smart parameter optimization using Bayesian search (Optuna). 10x faster than grid sweep."""
    from pathlib import Path
    from rift.strategy import discover_strategies, get_strategy
    from rift.historical_data import load_candles_smart, load_funding_smart
    from rift.data import normalize_coin as _nc
    import dataclasses

    pair = _nc(pair)

    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    from rift.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)

    _emit({"type": "progress", "pct": 5, "msg": f"Loading data for {strategy_name} on {pair}..."})

    df = load_candles_smart(pair, interval)
    if df is None or len(df) < 200:
        _emit({"type": "error", "msg": f"Insufficient data for {pair} {interval}"})
        sys.exit(1)
    funding_df = load_funding_smart(pair)

    import polars as pl
    oi_path = Path(__file__).parent.parent.parent.parent.parent / "packages" / "cli" / "data" / pair / "oi_daily.parquet"
    oi_df = pl.read_parquet(oi_path) if oi_path.exists() else None

    # Build param ranges from strategy config
    if not strategy_cls.config_class:
        _emit({"type": "error", "msg": "Strategy has no config class — nothing to optimize"})
        sys.exit(1)

    param_ranges = {}
    skip_params = {"leverage", "stop_loss_pct", "trailing_stop", "trail_atr_mult"}
    for f in dataclasses.fields(strategy_cls.config_class):
        if f.name in skip_params or f.default is dataclasses.MISSING:
            continue
        val = f.default
        if isinstance(val, bool):
            continue
        elif isinstance(val, int) and val > 1:
            param_ranges[f.name] = (max(2, val // 3), val * 3, max(1, val // 5))
        elif isinstance(val, float) and val > 0:
            param_ranges[f.name] = (val * 0.3, val * 3.0, val * 0.1)

    if not param_ranges:
        _emit({"type": "error", "msg": "No tunable parameters found"})
        sys.exit(1)

    _emit({"type": "progress", "pct": 10, "msg": f"Optimizing {len(param_ranges)} parameters over {n_trials} trials..."})

    from rift.smart_optimize import smart_sweep

    result = smart_sweep(
        strategy_cls=strategy_cls,
        df=df,
        param_ranges=param_ranges,
        funding_df=funding_df,
        oi_df=oi_df,
        pair=pair,
        interval=interval,
        n_trials=n_trials,
        optimize_target=target,
        on_progress=lambda done, total, best: _emit({
            "type": "progress", "pct": 10 + int(done / total * 85),
            "msg": f"Trial {done}/{total} — best {target}: {best:.2f}"
        }),
    )

    _emit({"type": "progress", "pct": 100, "msg": "Done"})
    _emit({
        "type": "result",
        "command": "smart-sweep",
        "strategy": strategy_name,
        "pair": pair,
        "best_params": result.best_params,
        "best_return": round(result.best_return, 2),
        "best_sharpe": round(result.best_sharpe, 2),
        "best_win_rate": round(result.best_win_rate, 2),
        "best_max_dd": round(result.best_max_dd, 2),
        "trials": result.n_trials,
        "improvement": result.improvement_vs_default,
    })


@app.command("feature-importance")
def feature_importance_cmd(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", help="Timeframe"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategy directory"),
) -> None:
    """Discover which features predict profitable trades using XGBoost."""
    from pathlib import Path
    from rift.strategy import discover_strategies, get_strategy
    from rift.historical_data import load_candles_smart, load_funding_smart
    from rift.smart_optimize import feature_importance
    from rift.data import normalize_coin as _nc
    import polars as pl

    pair = _nc(pair)
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    from rift.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    strategy_cls = get_strategy(strategy_name)
    df = load_candles_smart(pair, interval)
    funding_df = load_funding_smart(pair)
    oi_path = Path(__file__).parent.parent.parent.parent.parent / "packages" / "cli" / "data" / pair / "oi_daily.parquet"
    oi_df = pl.read_parquet(oi_path) if oi_path.exists() else None

    _emit({"type": "progress", "pct": 20, "msg": "Training XGBoost model..."})

    result = feature_importance(strategy_cls, df, funding_df, oi_df, pair, interval)

    _emit({"type": "progress", "pct": 100, "msg": "Done"})
    _emit({"type": "result", "command": "feature-importance", "strategy": strategy_name, "pair": pair, "features": result})


@app.command("tearsheet")
def tearsheet_cmd(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", help="Timeframe"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategy directory"),
) -> None:
    """Generate a professional HTML performance tearsheet."""
    from pathlib import Path
    from rift.strategy import discover_strategies, get_strategy
    from rift.historical_data import load_candles_smart, load_funding_smart
    from rift.backtest import run_backtest
    from rift.reports import generate_tearsheet
    from rift.data import normalize_coin as _nc
    import polars as pl

    pair = _nc(pair)
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    from rift.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    strategy_cls = get_strategy(strategy_name)
    df = load_candles_smart(pair, interval)
    funding_df = load_funding_smart(pair)
    oi_path = Path(__file__).parent.parent.parent.parent.parent / "packages" / "cli" / "data" / pair / "oi_daily.parquet"
    oi_df = pl.read_parquet(oi_path) if oi_path.exists() else None

    _emit({"type": "progress", "pct": 20, "msg": "Running backtest..."})
    strategy = strategy_cls()
    bt = run_backtest(strategy=strategy, df=df, strategy_name=strategy_name,
                      pair=pair, interval=interval, funding_df=funding_df, oi_df=oi_df, silent=True)

    _emit({"type": "progress", "pct": 70, "msg": "Generating tearsheet..."})
    path = generate_tearsheet(bt.equity_curve, f"{strategy_name}_{pair}_{interval}")

    _emit({"type": "progress", "pct": 100, "msg": "Done"})
    _emit({"type": "result", "command": "tearsheet", "path": path, "return": round(bt.total_return_pct, 2), "sharpe": round(bt.sharpe_ratio, 2)})


@app.command("quick-test")
def quick_test(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("", "--tf", "--interval", help="Timeframe (auto from config)"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity"),
    leverage: float = typer.Option(1.0, "--leverage", help="Leverage"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategies directory"),
    change_desc: str = typer.Option("", "--change", help="Description of what changed"),
) -> None:
    """Fast backtest with delta comparison to last test. The core feedback loop."""
    from rift.data import load_candles, load_funding_rates, fetch_candles, save_candles, fetch_funding_rates, save_funding_rates
    from rift.backtest import run_backtest
    from rift.strategy import discover_strategies, get_strategy
    from rift.workbench import (
        GENERATED_DIR, log_experiment, get_last_experiment,
        StrategyConfig, list_configs,
    )

    # Discover strategies from all sources
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    # Also discover from workbench generated strategies
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    # Load strategy
    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)

    # Auto-detect timeframe
    if not interval:
        # Check workbench config first
        if strategy_name in list_configs():
            config = StrategyConfig.load(strategy_name)
            interval = config.timeframe
        else:
            interval = strategy_cls.default_interval

    strategy = strategy_cls()

    # Auto-fetch data
    df = load_candles(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "progress", "pct": 0, "msg": f"Fetching {pair} {interval}..."})
        try:
            df = fetch_candles(pair, interval)
            if len(df) > 0:
                save_candles(df, pair, interval)
        except Exception as e:
            _emit({"type": "error", "msg": f"Failed to fetch data: {e}"})
            sys.exit(1)

    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No data for {pair} {interval}"})
        sys.exit(1)

    # Funding rates
    funding_df = load_funding_rates(pair)
    if funding_df is None or len(funding_df) == 0:
        try:
            funding_df = fetch_funding_rates(pair, start_time=int(df["timestamp"].min()))
            if len(funding_df) > 0:
                save_funding_rates(funding_df, pair)
        except Exception:
            pass

    # Run fast backtest
    result = run_backtest(
        strategy=strategy, df=df, strategy_name=strategy_name,
        pair=pair, interval=interval, initial_equity=equity, leverage=leverage,
        funding_df=funding_df, silent=True,
    )

    # Get last experiment for delta comparison
    last = get_last_experiment(strategy_name, pair)

    # Compute deltas
    delta = {}
    if last:
        for key in ("return_pct", "sharpe", "num_trades", "win_rate", "max_drawdown", "profit_factor"):
            current_val = {
                "return_pct": result.total_return_pct,
                "sharpe": result.sharpe_ratio,
                "num_trades": result.num_trades,
                "win_rate": result.win_rate,
                "max_drawdown": result.max_drawdown_pct,
                "profit_factor": result.profit_factor,
            }[key]
            last_val = last.get(key, 0) or 0
            delta[key] = round(current_val - last_val, 4)

    # Log experiment
    config_dict = {}
    if strategy_name in list_configs():
        config_dict = StrategyConfig.load(strategy_name).to_dict()

    exp_id = log_experiment(
        strategy_name=strategy_name,
        version=config_dict.get("version", 1),
        pair=pair,
        timeframe=interval,
        config=config_dict,
        results={
            "total_return_pct": result.total_return_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "num_trades": result.num_trades,
            "win_rate": result.win_rate,
            "max_drawdown_pct": result.max_drawdown_pct,
            "profit_factor": result.profit_factor,
            "total_funding": result.total_funding,
        },
        change_description=change_desc,
    )

    _emit({
        "type": "result",
        "command": "quick-test",
        "experiment_id": exp_id,
        "strategy": strategy_name,
        "pair": pair,
        "interval": interval,
        "return_pct": round(result.total_return_pct, 2),
        "sharpe": round(result.sharpe_ratio, 4),
        "num_trades": result.num_trades,
        "win_rate": round(result.win_rate, 2),
        "max_drawdown": round(result.max_drawdown_pct, 2),
        "profit_factor": round(result.profit_factor, 2),
        "total_funding": round(result.total_funding, 2),
        "delta": _sanitize_for_json(delta),
        "has_previous": last is not None,
    })
    _hint(f"Next: run full validation with 'rift research {strategy_name} --pair {pair}'")


@app.command("experiments")
def experiments(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    limit: int = typer.Option(20, "--limit", help="Number of experiments to show"),
) -> None:
    """Show experiment history for a strategy."""
    from rift.workbench import get_experiments

    exps = get_experiments(strategy_name, limit)
    _emit({"type": "result", "command": "experiments", "strategy": strategy_name, "experiments": exps})


@app.command("experiment-revert")
def experiment_revert(
    experiment_id: int = typer.Argument(..., help="Experiment ID to revert to"),
) -> None:
    """Revert a strategy config to a previous experiment's state."""
    from rift.workbench import get_experiment_config, StrategyConfig, generate_and_save

    config_dict = get_experiment_config(experiment_id)
    if config_dict is None:
        _emit({"type": "error", "msg": f"Experiment #{experiment_id} not found or has no config."})
        sys.exit(1)

    if not config_dict.get("name"):
        _emit({"type": "error", "msg": f"Experiment #{experiment_id} has no strategy config (may be a validated strategy)."})
        sys.exit(1)

    config = StrategyConfig.from_dict(config_dict)
    config.bump_version()
    path = generate_and_save(config)

    _emit({
        "type": "result",
        "command": "experiment-revert",
        "experiment_id": experiment_id,
        "name": config.name,
        "version": config.version,
        "generated_path": str(path),
    })


@app.command("save-optimized")
def save_optimized(
    base_strategy: str = typer.Argument(..., help="Base strategy name to copy from"),
    new_name: str = typer.Argument(..., help="Name for the new strategy"),
    params_json: str = typer.Argument(..., help="JSON config overrides"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategies directory"),
) -> None:
    """Save an optimized strategy by copying the base and replacing config defaults."""
    import dataclasses
    import re
    from rift.strategy import discover_strategies, get_strategy
    from rift.workbench import GENERATED_DIR

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    # Find the base strategy's source file
    try:
        strategy_cls = get_strategy(base_strategy)
    except KeyError as e:
        _emit({"type": "error", "msg": str(e).strip('"')})
        sys.exit(1)

    # Get the source file
    import inspect
    source_file = inspect.getfile(strategy_cls)
    source_code = Path(source_file).read_text()

    # Parse the overrides
    params = json.loads(params_json)

    # Replace the @register name
    source_code = re.sub(
        r'@register\(["\']' + re.escape(base_strategy) + r'["\']\)',
        f'@register("{new_name}")',
        source_code,
    )

    # Replace the class name
    old_class_name = strategy_cls.__name__
    new_class_name = "".join(word.capitalize() for word in new_name.split("_"))
    source_code = source_code.replace(f"class {old_class_name}(", f"class {new_class_name}(")

    # Replace config defaults with optimized values
    if strategy_cls.config_class:
        for field in dataclasses.fields(strategy_cls.config_class):
            if field.name in params:
                new_val = params[field.name]
                # Format the new value
                if isinstance(field.default, bool):
                    new_repr = repr(bool(new_val))
                elif isinstance(field.default, int) and not isinstance(field.default, bool):
                    new_repr = str(int(new_val))
                elif isinstance(field.default, float):
                    new_repr = str(float(new_val))
                else:
                    continue

                # Robust pattern: match field_name: type = ANY_VALUE (until end of line or comment)
                # This handles any float notation (0.02, 2e-2, 0.020, etc.)
                pattern = re.compile(
                    r'(\b' + re.escape(field.name) + r'\s*:\s*\w+\s*=\s*)([^\s#]+)'
                )
                new_code, count = pattern.subn(r'\g<1>' + new_repr, source_code)
                if count > 0:
                    source_code = new_code
                else:
                    _emit({"type": "progress", "pct": 0, "msg": f"Warning: could not find {field.name} in source to override"})

    # Add a comment header
    params_str = ", ".join(f"{k}={v}" for k, v in params.items())
    header = f'"""Optimized from {base_strategy}.\n\nParams: {params_str}\n"""\n\n'
    # Replace the original docstring or prepend
    if source_code.startswith('"""'):
        # Find end of first docstring
        end = source_code.index('"""', 3) + 3
        source_code = header + source_code[end:].lstrip()
    else:
        source_code = header + source_code

    # Save to workbench strategies directory
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / f"{new_name}.py"
    out_path.write_text(source_code)

    _emit({
        "type": "result",
        "command": "save-optimized",
        "name": new_name,
        "base": base_strategy,
        "params": params,
        "path": str(out_path),
    })


@app.command("indicator-stats")
def indicator_stats(
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", help="Timeframe"),
    indicator: str = typer.Option("all", "--indicator", help="Specific indicator or 'all'"),
) -> None:
    """Compute real market statistics for indicators from cached data."""
    import numpy as np
    from rift.data import load_candles, load_funding_rates, fetch_candles, save_candles, fetch_funding_rates, save_funding_rates

    # Auto-fetch if needed
    df = load_candles(pair, interval)
    if df is None or len(df) == 0:
        _emit({"type": "progress", "pct": 0, "msg": f"Fetching {pair} {interval}..."})
        try:
            df = fetch_candles(pair, interval)
            if len(df) > 0:
                save_candles(df, pair, interval)
        except Exception as e:
            _emit({"type": "error", "msg": f"Cannot fetch data: {e}"})
            sys.exit(1)

    if df is None or len(df) == 0:
        _emit({"type": "error", "msg": f"No data for {pair} {interval}"})
        sys.exit(1)

    closes = df["close"].to_numpy().astype(float)
    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    volumes = df["volume"].to_numpy().astype(float)

    stats: dict = {"pair": pair, "interval": interval, "candles": len(df)}

    # Funding rate stats
    if indicator in ("all", "funding_rate"):
        funding_df = load_funding_rates(pair)
        if funding_df is None or len(funding_df) == 0:
            try:
                funding_df = fetch_funding_rates(pair, start_time=int(df["timestamp"].min()))
                if len(funding_df) > 0:
                    save_funding_rates(funding_df, pair)
            except Exception:
                pass

        if funding_df is not None and len(funding_df) > 0:
            rates = funding_df["funding_rate"].to_numpy().astype(float)
            abs_rates = np.abs(rates)
            stats["funding_rate"] = {
                "min": round(float(np.min(rates)), 8),
                "max": round(float(np.max(rates)), 8),
                "mean": round(float(np.mean(rates)), 8),
                "median": round(float(np.median(rates)), 8),
                "p25": round(float(np.percentile(abs_rates, 25)), 8),
                "p50": round(float(np.percentile(abs_rates, 50)), 8),
                "p75": round(float(np.percentile(abs_rates, 75)), 8),
                "p90": round(float(np.percentile(abs_rates, 90)), 8),
                "p95": round(float(np.percentile(abs_rates, 95)), 8),
                "std": round(float(np.std(rates)), 8),
                "positive_pct": round(float(np.mean(rates > 0) * 100), 1),
                "recommended": round(float(np.percentile(abs_rates, 75)), 8),
                "description": "Hourly funding rate. Positive = longs pay shorts.",
            }

    # RSI stats
    if indicator in ("all", "rsi"):
        from rift.backtest import _compute_indicator
        from rift.strategy import RSI
        rsi_series = _compute_indicator("rsi", RSI(14), closes, highs, lows, volumes)
        valid_rsi = rsi_series[~np.isnan(rsi_series)]
        if len(valid_rsi) > 0:
            stats["rsi"] = {
                "min": round(float(np.min(valid_rsi)), 1),
                "max": round(float(np.max(valid_rsi)), 1),
                "mean": round(float(np.mean(valid_rsi)), 1),
                "median": round(float(np.median(valid_rsi)), 1),
                "p10": round(float(np.percentile(valid_rsi, 10)), 1),
                "p25": round(float(np.percentile(valid_rsi, 25)), 1),
                "p75": round(float(np.percentile(valid_rsi, 75)), 1),
                "p90": round(float(np.percentile(valid_rsi, 90)), 1),
                "pct_below_30": round(float(np.mean(valid_rsi < 30) * 100), 1),
                "pct_above_70": round(float(np.mean(valid_rsi > 70) * 100), 1),
                "recommended_oversold": round(float(np.percentile(valid_rsi, 15)), 0),
                "recommended_overbought": round(float(np.percentile(valid_rsi, 85)), 0),
                "description": "Relative Strength Index (14). <30 oversold, >70 overbought.",
            }

    # VWAP z-score stats
    if indicator in ("all", "vwap_zscore"):
        from rift.backtest import _compute_indicator
        from rift.strategy import VWAP, VWAPStd
        period = 144 if interval == "30m" else 72
        vwap_series = _compute_indicator("vwap", VWAP(period), closes, highs, lows, volumes)
        vwap_std_series = _compute_indicator("vwap_std", VWAPStd(period), closes, highs, lows, volumes)
        valid = (~np.isnan(vwap_series)) & (~np.isnan(vwap_std_series)) & (vwap_std_series > 0)
        if np.sum(valid) > 0:
            zscores = (closes[valid] - vwap_series[valid]) / vwap_std_series[valid]
            stats["vwap_zscore"] = {
                "min": round(float(np.min(zscores)), 2),
                "max": round(float(np.max(zscores)), 2),
                "mean": round(float(np.mean(zscores)), 2),
                "std": round(float(np.std(zscores)), 2),
                "p5": round(float(np.percentile(zscores, 5)), 2),
                "p95": round(float(np.percentile(zscores, 95)), 2),
                "pct_beyond_2": round(float(np.mean(np.abs(zscores) > 2) * 100), 1),
                "pct_beyond_3": round(float(np.mean(np.abs(zscores) > 3) * 100), 1),
                "recommended_entry": round(float(np.percentile(np.abs(zscores), 95)), 1),
                "description": f"VWAP deviation in standard deviations ({period}-period VWAP).",
            }

    # ADX stats
    if indicator in ("all", "adx"):
        from rift.backtest import _compute_indicator
        from rift.strategy import ADX
        adx_series = _compute_indicator("adx", ADX(14), closes, highs, lows, volumes)
        valid_adx = adx_series[~np.isnan(adx_series)]
        if len(valid_adx) > 0:
            stats["adx"] = {
                "min": round(float(np.min(valid_adx)), 1),
                "max": round(float(np.max(valid_adx)), 1),
                "mean": round(float(np.mean(valid_adx)), 1),
                "median": round(float(np.median(valid_adx)), 1),
                "pct_above_25": round(float(np.mean(valid_adx > 25) * 100), 1),
                "pct_above_40": round(float(np.mean(valid_adx > 40) * 100), 1),
                "recommended": 25.0,
                "description": "Average Directional Index. >25 = trending, >40 = strong trend.",
            }

    # Volume ratio stats
    if indicator in ("all", "volume"):
        from rift.backtest import _compute_indicator
        from rift.strategy import VolRatio
        vol_series = _compute_indicator("vol_ratio", VolRatio(20), closes, highs, lows, volumes)
        valid_vol = vol_series[~np.isnan(vol_series)]
        if len(valid_vol) > 0:
            stats["volume"] = {
                "mean": round(float(np.mean(valid_vol)), 2),
                "median": round(float(np.median(valid_vol)), 2),
                "p75": round(float(np.percentile(valid_vol, 75)), 2),
                "p90": round(float(np.percentile(valid_vol, 90)), 2),
                "p95": round(float(np.percentile(valid_vol, 95)), 2),
                "recommended": round(float(np.percentile(valid_vol, 75)), 1),
                "description": "Volume relative to 20-period average. 1.0 = average.",
            }

    # EMA distance stats
    if indicator in ("all", "ema"):
        from rift.backtest import _compute_indicator
        from rift.strategy import EMA
        for period in [50, 100, 200]:
            ema_series = _compute_indicator(f"ema_{period}", EMA(period), closes, highs, lows, volumes)
            valid = ~np.isnan(ema_series)
            if np.sum(valid) > 0:
                pct_dist = ((closes[valid] - ema_series[valid]) / ema_series[valid]) * 100
                stats[f"ema_{period}"] = {
                    "mean_distance_pct": round(float(np.mean(pct_dist)), 2),
                    "median_distance_pct": round(float(np.median(pct_dist)), 2),
                    "pct_above": round(float(np.mean(pct_dist > 0) * 100), 1),
                    "description": f"EMA {period} — price distance from moving average.",
                }

    # ATR stats
    if indicator in ("all", "atr"):
        from rift.backtest import _compute_indicator
        from rift.strategy import ATR
        atr_series = _compute_indicator("atr", ATR(14), closes, highs, lows, volumes)
        valid_atr = atr_series[~np.isnan(atr_series)]
        if len(valid_atr) > 0:
            atr_pct = (valid_atr / closes[~np.isnan(atr_series)]) * 100
            stats["atr"] = {
                "mean_pct": round(float(np.mean(atr_pct)), 2),
                "median_pct": round(float(np.median(atr_pct)), 2),
                "p90_pct": round(float(np.percentile(atr_pct, 90)), 2),
                "description": "Average True Range as % of price. Measures volatility.",
            }

    _emit({"type": "result", "command": "indicator-stats", **stats})


@app.command("history")
def session_history(
    limit: int = typer.Option(10, "--limit", help="Number of sessions to return"),
    strategy: str = typer.Option("", "--strategy", help="Filter by strategy name"),
) -> None:
    """List past algo trading sessions with P&L, trades, and outcomes."""
    algo_dir = Path.home() / ".rift" / "algo_sessions"

    sessions = []

    def _parse_session(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text())
            summary = data.get("summary", data)
            return {
                "mode": "algo",
                "strategy": summary.get("strategy", ""),
                "pair": summary.get("pair", ""),
                "started_at": summary.get("started_at", ""),
                "ended_at": summary.get("ended_at", ""),
                "num_trades": summary.get("num_trades", len(data.get("trades", []))),
                "return_pct": round(summary.get("total_return_pct", summary.get("total_pnl_pct", 0)), 2),
                "final_equity": round(summary.get("final_equity", 0), 2),
                "initial_equity": round(summary.get("initial_equity", 10000), 2),
                "log_file": str(path),
            }
        except Exception:
            return None

    if algo_dir.exists():
        for f in sorted(algo_dir.glob("*.json"), reverse=True):
            s = _parse_session(f)
            if s:
                sessions.append(s)

    sessions.sort(key=lambda x: x.get("started_at", ""), reverse=True)

    if strategy:
        sessions = [s for s in sessions if strategy in s.get("strategy", "")]

    sessions = sessions[:limit]
    _emit({"type": "result", "command": "history", "sessions": sessions, "total": len(sessions)})


