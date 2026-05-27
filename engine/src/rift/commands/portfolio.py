"""Portfolio backtest / VaR / TCA / attribution / report commands — extracted from cli.py in Phase 6.

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


@app.command("portfolio-backtest")
def portfolio_backtest(
    config_path: str = typer.Argument(..., help="Path to portfolio.yaml config file"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
) -> None:
    """Run a portfolio backtest with multiple strategies."""
    from rift.portfolio import load_portfolio_config, run_portfolio_backtest

    try:
        config = load_portfolio_config(config_path)
    except Exception as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    def on_progress(pct: int, msg: str) -> None:
        _emit({"type": "progress", "pct": pct, "msg": msg})

    try:
        result = run_portfolio_backtest(config, strategies_dir, on_progress)
    except Exception as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    _emit({"type": "result", "command": "portfolio-backtest", **result.to_dict()})


@app.command("portfolio-matrix")
def portfolio_matrix(
    pairs: str = typer.Option("BTC,ETH,SOL", "--pairs", help="Comma-separated coins to analyze"),
    strategies_list: str = typer.Option("", "--strategies", help="Comma-separated strategies (auto-discovers if empty)"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity per strategy"),
) -> None:
    """Generate monthly P&L matrix, correlation matrix, and regime analysis across all strategies and coins."""
    from pathlib import Path
    from rift.strategy import discover_strategies as _discover, get_strategy, list_strategies as _ls
    from rift.backtest import run_backtest
    from rift.historical_data import load_candles_smart, load_funding_smart

    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies", Path(__file__).parent / "strategies"]
    from rift.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    _discover(dirs)

    coins = [c.strip().upper() for c in pairs.split(",")]

    if strategies_list:
        strats = [s.strip() for s in strategies_list.split(",")]
    else:
        strats = list(_ls().keys())
        if not strats:
            _emit({"type": "error", "msg": "No strategies found. Create one with 'rift workbench-create' or place .py files in strategies/."})
            return

    _emit({"type": "progress", "pct": 0, "msg": f"Running {len(strats)} strategies × {len(coins)} coins..."})

    # Run all strategy/coin combinations
    results = []
    total = len(strats) * len(coins)
    done = 0

    for strat_name in strats:
        try:
            strategy_cls = get_strategy(strat_name)
        except KeyError:
            continue

        for coin in coins:
            done += 1
            strategy = strategy_cls()
            interval = strategy.default_interval
            df = load_candles_smart(coin, interval)
            funding_df = load_funding_smart(coin)
            if df is None or len(df) == 0:
                continue

            lev = strategy.config.leverage if hasattr(strategy.config, 'leverage') else 1.0

            try:
                bt = run_backtest(
                    strategy=strategy, df=df, strategy_name=strat_name,
                    pair=coin, interval=interval, initial_equity=equity,
                    leverage=lev, funding_df=funding_df, silent=True,
                )
            except Exception:
                continue

            if bt.num_trades == 0:
                continue

            days = len(df) * {'5m': 5, '15m': 15, '1h': 60}.get(interval, 60) / 60 / 24

            results.append({
                "strategy": strat_name,
                "coin": coin,
                "interval": interval,
                "days": round(days),
                "total_return_pct": round(bt.total_return_pct, 1),
                "annualized_return_pct": round(bt.total_return_pct / (days / 365), 1),
                "num_trades": bt.num_trades,
                "win_rate": round(bt.win_rate, 1),
                "sharpe": round(bt.sharpe_ratio, 2),
                "max_drawdown_pct": round(bt.max_drawdown_pct, 1),
                "profit_factor": round(bt.profit_factor, 2),
                "monthly_returns": {k: round(v, 1) for k, v in bt.monthly_returns.items()},
                "equity_curve": bt.equity_curve,
            })

            pct = int(done / total * 90)
            _emit({"type": "progress", "pct": pct, "msg": f"{strat_name} {coin}: {bt.total_return_pct:+.1f}%"})

    if not results:
        _emit({"type": "error", "msg": "No valid results. Check data availability."})
        return

    # Build correlation matrix from equity curves
    import numpy as np
    correlation_matrix = None
    if len(results) > 1:
        min_len = min(len(r["equity_curve"]) for r in results)
        returns_list = []
        labels = []
        for r in results:
            arr = np.array(r["equity_curve"][:min_len])
            rets = np.diff(arr) / np.where(arr[:-1] == 0, 1, arr[:-1])
            rets = np.where(np.isnan(rets), 0, rets)
            returns_list.append(rets)
            labels.append(f"{r['strategy']}_{r['coin']}")

        corr = np.corrcoef(returns_list)
        correlation_matrix = {
            "labels": labels,
            "matrix": [[round(float(corr[i][j]), 3) for j in range(len(labels))] for i in range(len(labels))],
        }

    # Build monthly matrix: month × (strategy_coin) → return
    all_months = set()
    for r in results:
        all_months.update(r["monthly_returns"].keys())
    sorted_months = sorted(all_months)

    monthly_matrix = {}
    for month in sorted_months:
        monthly_matrix[month] = {}
        for r in results:
            key = f"{r['strategy']}_{r['coin']}"
            monthly_matrix[month][key] = r["monthly_returns"].get(month, 0.0)

    # Strip equity curves from output (too large for JSON)
    for r in results:
        del r["equity_curve"]

    _emit({"type": "progress", "pct": 100, "msg": "Portfolio matrix complete"})

    # Identify profitable pairs
    profitable = [r for r in results if r["total_return_pct"] > 0]
    profitable.sort(key=lambda x: x["sharpe"], reverse=True)

    _emit({
        "type": "result",
        "command": "portfolio-matrix",
        "summary": {
            "total_combinations": len(results),
            "profitable_combinations": len(profitable),
            "best_sharpe": profitable[0] if profitable else None,
        },
        "results": results,
        "monthly_matrix": monthly_matrix,
        "correlation_matrix": correlation_matrix,
        "profitable_ranked": [
            {
                "strategy": r["strategy"],
                "coin": r["coin"],
                "return": r["total_return_pct"],
                "annualized": r["annualized_return_pct"],
                "sharpe": r["sharpe"],
                "trades": r["num_trades"],
                "win_rate": r["win_rate"],
            }
            for r in profitable
        ],
    })

    # Write validated edge cache for Scout auto-pickup
    from pathlib import Path
    edge_cache_path = Path.home() / ".rift" / "validated_edge.json"
    edge_cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Build VALIDATED_EDGE structure: {strategy: {coin: {return_pct, sharpe, trades}}}
    validated_edge = {}
    # Build BLACKLISTED_COINS: coins that lost on ALL tested strategies
    coin_results: dict[str, list] = {}  # coin → list of returns
    for r in results:
        coin_results.setdefault(r["coin"], []).append(r["total_return_pct"])
        if r["total_return_pct"] > 0 and r["sharpe"] > 0.2:
            validated_edge.setdefault(r["strategy"], {})[r["coin"]] = {
                "return_pct": r["total_return_pct"],
                "sharpe": r["sharpe"],
                "trades": r["num_trades"],
            }

    blacklisted = [coin for coin, rets in coin_results.items() if all(r < 0 for r in rets)]

    import json as _json

    cache_data = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "validated_edge": validated_edge,
        "blacklisted_coins": blacklisted,
        "validated_coins": sorted(set(c for edges in validated_edge.values() for c in edges)),
    }
    edge_cache_path.write_text(_json.dumps(cache_data, indent=2))
    _emit({"type": "info", "msg": f"Validated edge cache written to {edge_cache_path}"})
    _hint("Validated edge cache updated. Scout will boost these coins. Run 'rift scout' to scan.")


@app.command("pairs-backtest")
def pairs_backtest(
    asset_a: str = typer.Option("BTC", "--a", help="First asset (e.g. BTC)"),
    asset_b: str = typer.Option("ETH", "--b", help="Second asset (e.g. ETH)"),
    interval: str = typer.Option("1h", "--tf", "--interval", help="Candle interval"),
    equity: float = typer.Option(10000.0, "--equity", help="Starting equity"),
    lookback: int = typer.Option(168, "--lookback", help="Rolling window for z-score (hours)"),
    entry_z: float = typer.Option(2.0, "--entry-z", help="Z-score entry threshold"),
    exit_z: float = typer.Option(0.5, "--exit-z", help="Z-score exit threshold"),
    stop_z: float = typer.Option(4.0, "--stop-z", help="Z-score stop loss"),
    max_hold: int = typer.Option(72, "--max-hold", help="Max hold time in candles"),
) -> None:
    """Run a pairs trading backtest (e.g. BTC/ETH spread)."""
    from rift.data import load_candles, load_funding_rates
    from rift.pairs import run_pairs_backtest

    df_a = load_candles(asset_a, interval)
    df_b = load_candles(asset_b, interval)

    if df_a is None or len(df_a) == 0:
        _emit({"type": "error", "msg": f"No cached data for {asset_a} {interval}. Run 'rift data fetch --pair {asset_a}-PERP --tf {interval}' first."})
        sys.exit(1)
    if df_b is None or len(df_b) == 0:
        _emit({"type": "error", "msg": f"No cached data for {asset_b} {interval}. Run 'rift data fetch --pair {asset_b}-PERP --tf {interval}' first."})
        sys.exit(1)

    funding_a = load_funding_rates(asset_a)
    funding_b = load_funding_rates(asset_b)

    def on_progress(pct: int, msg: str) -> None:
        _emit({"type": "progress", "pct": pct, "msg": msg})

    try:
        result = run_pairs_backtest(
            df_a=df_a, df_b=df_b,
            asset_a=asset_a, asset_b=asset_b,
            interval=interval,
            initial_equity=equity,
            lookback=lookback,
            entry_zscore=entry_z,
            exit_zscore=exit_z,
            stop_zscore=stop_z,
            max_hold_candles=max_hold,
            funding_a=funding_a,
            funding_b=funding_b,
            on_progress=on_progress,
        )
    except ValueError as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)

    _emit({"type": "result", "command": "pairs-backtest", **result.to_dict(), "export": result.to_export_dict()})


@app.command("portfolio-start")
def portfolio_start(
    config: str = typer.Option("", "--config", help="Path to portfolio.yaml"),
    account_address: str = typer.Option("", "--account", help="Main account address"),
    daemon: bool = typer.Option(False, "--daemon", help="Run as background daemon"),
) -> None:
    """Start the portfolio supervisor to manage multiple live strategies."""
    import os
    from rift.supervisor import run_supervisor, is_supervisor_running

    if is_supervisor_running():
        _emit({"type": "result", "command": "portfolio-start", "status": "already_running"})
        return

    from rift.config import get_env_var
    private_key = get_env_var("HYPERLIQUID_PRIVATE_KEY")

    if daemon:
        # Fork and run in background
        pid = os.fork()
        if pid > 0:
            _emit({"type": "result", "command": "portfolio-start", "status": "started", "pid": pid})
            return
        # Child process — redirect stdio and run
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)

    run_supervisor(
        config_path=config,
        private_key=private_key,
        account_address=account_address,
    )


@app.command("portfolio-status")
def portfolio_status() -> None:
    """Get status of the portfolio supervisor and all managed strategies."""
    from rift.supervisor import get_supervisor_status
    from rift.alerts import get_recent_alerts

    status = get_supervisor_status()
    alerts = get_recent_alerts(limit=10)

    _emit({
        "type": "result",
        "command": "portfolio-status",
        "supervisor": status,
        "recent_alerts": alerts,
    })


@app.command("portfolio-stop")
def portfolio_stop() -> None:
    """Stop the portfolio supervisor and all managed strategies."""
    from rift.supervisor import stop_supervisor

    result = stop_supervisor()
    _emit({"type": "result", "command": "portfolio-stop", **result})


@app.command("tca")
def tca(
    session: str = typer.Option("", "--session", help="Path to session log (default: most recent)"),
) -> None:
    """Run Transaction Cost Analysis on live trading sessions."""
    from rift.tca import analyze_session_log, analyze_all_sessions
    import dataclasses

    if session:
        report = analyze_session_log(session)
    else:
        report = analyze_all_sessions()

    _emit({"type": "result", "command": "tca", **dataclasses.asdict(report)})


@app.command("attribution")
def attribution(
    session: str = typer.Option("", "--session", help="Path to session log (default: all sessions)"),
) -> None:
    """Run P&L attribution — decompose returns into alpha, beta, funding, execution."""
    from rift.attribution import attribute_session_log, attribute_all_sessions
    import dataclasses

    if session:
        report = attribute_session_log(session)
    else:
        report = attribute_all_sessions()

    _emit({"type": "result", "command": "attribution", **dataclasses.asdict(report)})


@app.command("report")
def report(
    session: str = typer.Option("", "--session", help="Path to session log (default: most recent)"),
    period: str = typer.Option("all", "--period", help="Report period: all, daily, weekly"),
    portfolio: bool = typer.Option(False, "--portfolio", help="Generate portfolio-level report"),
) -> None:
    """Generate an HTML performance report with TCA and PnL attribution."""
    from rift.reports import generate_live_report, generate_portfolio_report

    if portfolio:
        path = generate_portfolio_report(period=period)
    else:
        path = generate_live_report(session_log_path=session)

    if path:
        _emit({"type": "result", "command": "report", "path": path})
    else:
        _emit({"type": "result", "command": "report", "path": "", "msg": "No session data found"})


@app.command("var")
def var_cmd(
    horizon: str = typer.Option("24h", "--horizon", help="VaR horizon: 1h, 24h, 7d"),
) -> None:
    """Compute Value at Risk from live trading history."""
    from rift.var import var_from_sessions
    import dataclasses
    report = var_from_sessions()
    report.horizon = horizon
    _emit({"type": "result", "command": "var", **dataclasses.asdict(report)})


