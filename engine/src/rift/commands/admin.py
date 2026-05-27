"""Setup, auth, health, version, proxy, watchdog commands — extracted from cli.py in Phase 6.

The user-facing command surface is unchanged. Each command is registered
on the shared Typer `app` in `rift.commands._shared`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import typer

from rift.commands._shared import app, _emit, _hint, _sanitize_for_json
from rift import __version__
from rift_core.config import parse_duration as _parse_duration


@app.command("strategies")
def list_strategies(
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Directory with strategy .py files"),
) -> None:
    """List available strategies."""
    from rift.strategy import discover_strategies, list_strategies as ls

    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    from rift.strategy import get_config_metadata

    strategies = ls()
    result = []
    for name, cls in strategies.items():
        config_meta = get_config_metadata(cls.config_class) if cls.config_class else {}

        result.append({
            "name": name,
            "class": cls.__name__,
            "config": config_meta,
            "default_interval": cls.default_interval,
            "doc": (cls.__doc__ or "").strip(),
        })

    _emit({"type": "result", "command": "strategies", "strategies": result})


@app.command("guide")
def guide() -> None:
    """Print the research-to-trade journey as a quick reference."""
    _emit({"type": "result", "command": "guide", "steps": [
        {"step": 0, "command": "rift sync",
         "description": "Download historical data from Hyperliquid S3. Prompts for AWS credentials on first run. ~$2."},
        {"step": 1, "command": "rift auth setup",
         "description": "Connect wallet via QR scan. Required for live trading, not needed for research."},
        {"step": 2, "command": "rift workbench-create <name> --template <t>",
         "description": "Create a new strategy from a template (funding, vwap_reversion, trend_follow, blank)."},
        {"step": 3, "command": "rift quick-test <strategy> --pair <COIN>",
         "description": "Fast backtest with delta tracking. Iterate on your strategy."},
        {"step": 4, "command": "rift backtest <strategy> --pair <COIN>",
         "description": "Full backtest with equity curve, metrics, and trade log."},
        {"step": 5, "command": "rift walk-forward <strategy> --pair <COIN> --wf 4m/1m",
         "description": "Walk-forward analysis to test out-of-sample robustness."},
        {"step": 6, "command": "rift research <strategy> --pair <COIN>",
         "description": "Full validation: backtest + walk-forward + Monte Carlo + multi-pair. Grades A-F."},
        {"step": 7, "command": "rift portfolio-matrix --pairs <COINS>",
         "description": "Cross-strategy/coin matrix. Updates validated edge cache for Scout."},
        {"step": 8, "command": "rift algo --pair <COIN>-PERP",
         "description": "Go live. Auto-discovers best strategy for your coin via COIN_CONFIGS."},
    ]})


# ─── SPOT TRADING ───────────────────────────────────────────


@app.command("watchdog")
def watchdog(
    daemon: bool = typer.Option(False, "--daemon", help="Run as background daemon"),
    interval_m: int = typer.Option(5, "--interval", help="Check interval in minutes"),
    coins: str = typer.Option("BTC,ETH,SOL,SUI", "--coins", help="Coins to watch"),
) -> None:
    """Monitor markets for notable conditions (vol spikes, funding extremes, etc.)."""

    from rift.data import get_info_client, fetch_market_context, normalize_coin

    coin_list = [normalize_coin(c.strip()) for c in coins.split(",")]
    events_dir = Path.home() / ".rift" / "watchdog"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_file = events_dir / "events.ndjson"

    if daemon:
        import subprocess
        cmd = [sys.executable, "-m", "rift.cli", "watchdog",
               "--interval", str(interval_m), "--coins", coins]
        log_file = events_dir / "watchdog.log"
        with open(log_file, "w") as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        (events_dir / "watchdog.pid").write_text(str(proc.pid))
        _emit({"type": "result", "command": "watchdog", "status": "daemon_started", "pid": proc.pid})
        return

    def _check_once():
        info = get_info_client()
        events = []
        for coin in coin_list:
            try:
                ctx = fetch_market_context(coin, info)
                if not ctx:
                    continue
                funding = ctx.get("funding", 0)
                if abs(funding) > 0.0003:
                    events.append({"event": "funding_extreme", "coin": coin,
                                  "value": round(funding * 100, 4), "msg": f"{coin} funding extreme: {funding*100:.4f}%"})
                premium = ctx.get("premium", 0)
                if abs(premium) > 0.005:
                    events.append({"event": "premium_divergence", "coin": coin,
                                  "value": round(premium * 100, 2), "msg": f"{coin} premium divergence: {premium*100:.2f}%"})
            except Exception:
                continue
        return events

    # Foreground: run once or loop
    if interval_m <= 0:
        events = _check_once()
        _emit({"type": "result", "command": "watchdog", "events": events})
        return

    _emit({"type": "status", "msg": f"Watchdog running every {interval_m}m on {len(coin_list)} coins..."})
    try:
        while True:
            events = _check_once()
            for evt in events:
                evt["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                with open(events_file, "a") as f:
                    f.write(json.dumps(evt) + "\n")
                _emit({"type": "alert", **evt})
            time.sleep(interval_m * 60)
    except KeyboardInterrupt:
        pass


@app.command("watchdog-events")
def watchdog_events(
    since: str = typer.Option("24h", "--since", help="Time window (e.g. 1h, 24h, 7d)"),
    coin: str = typer.Option("", "--coin", help="Filter by coin"),
) -> None:
    """Query recent watchdog events."""

    from datetime import datetime

    events_file = Path.home() / ".rift" / "watchdog" / "events.ndjson"
    if not events_file.exists():
        _emit({"type": "result", "command": "watchdog-events", "events": [], "msg": "No events yet. Run: rift watchdog"})
        return

    cutoff_s = time.time() - _parse_duration(since)
    events = []
    for line in events_file.read_text().splitlines():
        try:
            evt = json.loads(line.strip())
            ts_str = evt.get("timestamp", "")
            if ts_str:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").timestamp()
                if ts < cutoff_s:
                    continue
            if coin and coin.upper() not in evt.get("coin", "").upper():
                continue
            events.append(evt)
        except Exception:
            continue

    _emit({"type": "result", "command": "watchdog-events", "events": events, "total": len(events)})


@app.command("watchdog-stop")
def watchdog_stop() -> None:
    """Stop the watchdog daemon."""
    import os as _os, signal as _signal

    pid_file = Path.home() / ".rift" / "watchdog" / "watchdog.pid"
    if not pid_file.exists():
        _emit({"type": "result", "command": "watchdog-stop", "status": "not_running"})
        return
    pid = int(pid_file.read_text().strip())
    try:
        _os.kill(pid, _signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        _emit({"type": "result", "command": "watchdog-stop", "status": "stopped"})
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        _emit({"type": "result", "command": "watchdog-stop", "status": "already_stopped"})


@app.command("doctor")
def doctor() -> None:
    """Check system health."""


    checks = []

    # Python version
    checks.append({"name": "Python", "status": "ok", "detail": f"{sys.version.split()[0]}"})

    # Engine version
    checks.append({"name": "Engine", "status": "ok", "detail": __version__})

    # Dependencies
    try:
        import polars
        checks.append({"name": "Polars", "status": "ok", "detail": polars.__version__})
    except ImportError:
        checks.append({"name": "Polars", "status": "fail", "detail": "not installed"})

    try:
        import numpy
        checks.append({"name": "NumPy", "status": "ok", "detail": numpy.__version__})
    except ImportError:
        checks.append({"name": "NumPy", "status": "fail", "detail": "not installed"})

    try:
        import pyarrow
        checks.append({"name": "PyArrow", "status": "ok", "detail": pyarrow.__version__})
    except ImportError:
        checks.append({"name": "PyArrow", "status": "fail", "detail": "not installed"})

    # Proxy config
    from rift.config import get_proxy
    proxy = get_proxy()
    if proxy:
        checks.append({"name": "Proxy", "status": "ok", "detail": proxy})
    else:
        checks.append({"name": "Proxy", "status": "info", "detail": "not configured (direct connection)"})

    # Hyperliquid API
    try:
        from rift.data import get_info_client
        start = time.time()
        info = get_info_client()
        mids = info.all_mids()
        latency = round((time.time() - start) * 1000)
        via = " via proxy" if proxy else ""
        checks.append({"name": "Hyperliquid API", "status": "ok", "detail": f"{latency}ms latency, {len(mids)} pairs{via}"})
    except Exception as e:
        err = str(e)
        hint = ""
        if "connection" in err.lower() or "timeout" in err.lower() or "403" in err or "refused" in err.lower():
            hint = " — run: rift setup proxy"
        checks.append({"name": "Hyperliquid API", "status": "fail", "detail": f"{err}{hint}"})

    # Cached data
    from rift.data import list_cached_data
    cached = list_cached_data()
    if cached:
        total_candles = sum(d.get("rows", 0) for d in cached)
        checks.append({"name": "Cached Data", "status": "ok", "detail": f"{len(cached)} datasets, {total_candles:,} candles"})
    else:
        checks.append({"name": "Cached Data", "status": "warn", "detail": "no data cached yet"})

    # Strategies
    from rift.strategy import discover_strategies, list_strategies as ls
    strat_dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies", Path(__file__).parent / "strategies"]
    from rift.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        strat_dirs.append(GENERATED_DIR)
    discover_strategies(strat_dirs)
    strategies = ls()
    if strategies:
        checks.append({"name": "Strategies", "status": "ok", "detail": f"{len(strategies)} available"})
    else:
        checks.append({"name": "Strategies", "status": "warn", "detail": "none found — run `rift new <name>` to scaffold"})

    # ~/.rift/ directory
    rift_dir = Path.home() / ".rift"
    if rift_dir.exists():
        checks.append({"name": "RIFT home dir", "status": "ok", "detail": str(rift_dir)})
    else:
        checks.append({"name": "RIFT home dir", "status": "warn",
                       "detail": f"{rift_dir} does not exist — run `mkdir -p ~/.rift && cp .env.example ~/.rift/.env`"})

    # .env file + permissions
    env_path = rift_dir / ".env"
    if env_path.exists():
        try:
            mode = oct(env_path.stat().st_mode)[-3:]
            if mode == "600":
                checks.append({"name": ".env permissions", "status": "ok", "detail": f"{env_path} is 0600"})
            else:
                checks.append({"name": ".env permissions", "status": "warn",
                               "detail": f"{env_path} is {mode} — credentials may be world-readable. Fix: `chmod 600 {env_path}`"})
        except Exception as perm_err:
            checks.append({"name": ".env permissions", "status": "warn", "detail": f"could not stat: {perm_err}"})
    else:
        checks.append({"name": ".env file", "status": "info",
                       "detail": f"{env_path} not present — sync + live trading need it. `cp .env.example ~/.rift/.env`"})

    # AWS credentials (needed for `rift sync` from the HL S3 archive)
    aws_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    if not aws_key and env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith("AWS_ACCESS_KEY_ID="):
                    aws_key = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    if aws_key:
        checks.append({"name": "AWS credentials", "status": "ok", "detail": f"AWS_ACCESS_KEY_ID set ({aws_key[:6]}…)"})
    else:
        checks.append({"name": "AWS credentials", "status": "info",
                       "detail": "not set — required only for `rift sync` (historical data). Edit ~/.rift/.env to configure."})

    # Disk space for the data dir (warn if <500MB free)
    try:
        usage = shutil.disk_usage(str(rift_dir if rift_dir.exists() else rift_dir.parent))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 0.5:
            checks.append({"name": "Disk space", "status": "fail",
                           "detail": f"{free_gb:.2f}GB free — sync and algo logs will fail. Free up space."})
        elif free_gb < 2.0:
            checks.append({"name": "Disk space", "status": "warn",
                           "detail": f"{free_gb:.2f}GB free — tight for full historical sync (~1GB)."})
        else:
            checks.append({"name": "Disk space", "status": "ok", "detail": f"{free_gb:.1f}GB free"})
    except Exception as e:
        checks.append({"name": "Disk space", "status": "warn", "detail": f"could not query: {e}"})

    _emit({"type": "result", "command": "doctor", "checks": checks})


@app.command("check-api")
def check_api(
    proxy: str = typer.Option("", "--proxy", help="Proxy URL to test with"),
) -> None:
    """Test Hyperliquid API connectivity, optionally via a proxy."""


    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        if proxy:
            info.session.proxies = {"http": proxy, "https": proxy}

        start = time.time()
        mids = info.all_mids()
        latency = round((time.time() - start) * 1000)

        _emit({
            "type": "result",
            "command": "check-api",
            "status": "ok",
            "latency_ms": latency,
            "pairs": len(mids),
            "proxy": proxy or None,
        })
    except Exception as e:
        _emit({
            "type": "result",
            "command": "check-api",
            "status": "fail",
            "error": str(e),
            "proxy": proxy or None,
        })


@app.command("set-proxy")
def set_proxy_cmd(
    proxy_url: str = typer.Argument(..., help="Proxy URL (e.g. socks5://127.0.0.1:1080)"),
) -> None:
    """Save proxy configuration."""
    from rift.config import set_proxy
    set_proxy(proxy_url)
    _emit({"type": "result", "command": "set-proxy", "proxy": proxy_url})


@app.command("clear-proxy")
def clear_proxy_cmd() -> None:
    """Remove proxy configuration."""
    from rift.config import clear_proxy
    clear_proxy()
    _emit({"type": "result", "command": "clear-proxy"})


@app.command("health")
def health_check(
    strategy_name: str = typer.Argument(..., help="Strategy name"),
    pair: str = typer.Option("BTC", "--pair", help="Trading pair"),
    interval: str = typer.Option("1h", "--tf", help="Timeframe"),
    strategies_dir: str = typer.Option("", "--strategies-dir", help="Strategy directory"),
) -> None:
    """Check strategy health — detects edge decay, alpha loss, and execution degradation."""
    from pathlib import Path
    from rift.data import load_candles, load_funding_rates
    from rift.backtest import run_backtest
    from rift.strategy import discover_strategies, get_strategy
    from rift.health import run_health_check
    from rift.data import normalize_coin as _nc

    pair = _nc(pair)

    # Discover strategies
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

    _emit({"type": "progress", "pct": 10, "msg": f"Loading data for {strategy_name} on {pair}..."})

    # Load data
    from rift.historical_data import load_candles_smart, load_funding_smart
    df = load_candles_smart(pair, interval)
    if df is None or len(df) < 200:
        _emit({"type": "error", "msg": f"Insufficient data for {pair} {interval}"})
        sys.exit(1)

    funding_df = load_funding_smart(pair)
    oi_path = Path.home() / ".rift" / "data" / pair / "historical" / "oi_daily.parquet"
    if not oi_path.exists():
        oi_path = Path(__file__).parent.parent.parent.parent.parent / "packages" / "cli" / "data" / pair / "oi_daily.parquet"
    import polars as pl
    oi_df = pl.read_parquet(oi_path) if oi_path.exists() else None

    _emit({"type": "progress", "pct": 30, "msg": "Running baseline backtest..."})

    # Run full backtest to get baseline
    strategy = strategy_cls()
    bt = run_backtest(
        strategy=strategy, df=df, strategy_name=strategy_name,
        pair=pair, interval=interval, funding_df=funding_df, oi_df=oi_df,
        silent=True,
    )

    if bt.num_trades < 10:
        _emit({"type": "error", "msg": f"Only {bt.num_trades} trades — need at least 10 for health analysis"})
        sys.exit(1)

    _emit({"type": "progress", "pct": 60, "msg": f"Analyzing {bt.num_trades} trades..."})

    # Split trades: first 70% as baseline, last 30% as "recent"
    split = int(len(bt.trades) * 0.7)
    baseline_trades = bt.trades[:split]
    recent_trades = bt.trades[split:]

    # Market returns for factor decomposition
    import numpy as np
    closes = df["close"].to_numpy().astype(float)
    market_returns = list(np.diff(closes) / closes[:-1])

    # Run health check
    baseline_wr = len([t for t in baseline_trades if t.pnl > 0]) / len(baseline_trades) * 100 if baseline_trades else 50

    report = run_health_check(
        recent_trades=recent_trades,
        baseline_trades=baseline_trades,
        equity_curve=bt.equity_curve,
        market_returns=market_returns,
        baseline_win_rate=baseline_wr,
    )

    _emit({"type": "progress", "pct": 100, "msg": "Done"})

    _emit({
        "type": "result",
        "command": "health",
        "strategy": strategy_name,
        "pair": pair,
        "total_trades": bt.num_trades,
        "baseline_trades": len(baseline_trades),
        "recent_trades": len(recent_trades),
        **report.to_dict(),
    })


@app.command("auth")
def auth_cmd(
    action: str = typer.Argument("setup", help="Action: setup, status, clear"),
    key: str = typer.Option("", "--key", help="Hyperliquid API wallet private key (0x...)"),
    account: str = typer.Option("", "--account", help="Main wallet address"),
) -> None:
    """Set up or manage wallet authentication for trading.

    Works from any interface (terminal, MCP, mobile):
        rift auth setup --key 0x...
        rift auth status
        rift auth clear
    """
    from rift.trading_gates import require_auth, setup_auth, get_api_key, get_account_address
    from rift.config import ENV_PATH

    if action == "setup":
        if key:
            # Non-interactive setup (MCP-friendly)
            result = setup_auth(key, account)
            if result:
                _emit({"type": "result", "command": "auth", "status": "configured"})
            else:
                _emit({"type": "error", "msg": "Auth setup failed"})
        else:
            # Interactive setup (terminal)
            result = require_auth()
            if result:
                _emit({"type": "result", "command": "auth", "status": "configured"})
            else:
                _emit({"type": "error", "msg": "Auth setup cancelled"})

    elif action == "status":
        existing_key = get_api_key()
        existing_account = get_account_address()
        if existing_key:
            masked = existing_key[:6] + "..." + existing_key[-4:] if len(existing_key) > 10 else "***"
            _emit({"type": "result", "command": "auth", "status": "configured",
                   "key": masked, "account": existing_account,
                   "env_file": str(ENV_PATH)})
        else:
            _emit({"type": "result", "command": "auth", "status": "not_configured",
                   "hint": "Run: rift auth setup --key 0x..."})

    elif action == "clear":
        from rift.config import ENV_PATH as _env
        from rift.trading_gates import AUTH_FILE
        cleared = False
        # Clear from .env
        if _env.exists():
            lines = _env.read_text().splitlines()
            lines = [l for l in lines if not l.strip().startswith("HYPERLIQUID_")]
            _env.write_text("\n".join(lines) + "\n") if lines else _env.unlink()
            cleared = True
        # Clear legacy file
        if AUTH_FILE.exists():
            AUTH_FILE.unlink()
            cleared = True
        if cleared:
            _emit({"type": "result", "command": "auth", "status": "cleared"})
        else:
            _emit({"type": "result", "command": "auth", "status": "nothing_to_clear"})

    else:
        _emit({"type": "error", "msg": f"Unknown action: {action}. Use: setup, status, clear"})


@app.command("approve-builder-fee")
def approve_builder_fee_cmd(
    private_key: str = typer.Argument(..., help="Main wallet private key (0x...)"),
    account_address: str = typer.Option("", "--account", help="Account address (defaults to wallet address)"),
) -> None:
    """Approve RIFT's builder fee on-chain (one-time, main wallet required)."""
    from rift.builder_fee import approve_builder_fee, BUILDER_ADDRESS, BUILDER_FEE_DISPLAY

    if not account_address:
        from eth_account import Account
        wallet = Account.from_key(private_key)
        account_address = wallet.address

    _emit({"type": "progress", "pct": 0, "msg": f"Approving {BUILDER_FEE_DISPLAY} builder fee for {BUILDER_ADDRESS}..."})

    try:
        result = approve_builder_fee(private_key, account_address)

        # If an API wallet is already paired locally, update its credentials
        # file with builder_fee_approved=True so the TS CLI gate (hasFullSetup)
        # recognizes the wallet as ready for live trading.
        try:
            from rift_trade.api_wallet import load_api_wallet, save_api_wallet
            existing = load_api_wallet()
            if existing is not None and not existing.builder_fee_approved:
                updated = existing.model_copy(update={"builder_fee_approved": True})
                save_api_wallet(updated)
        except Exception:
            pass  # best-effort; don't fail the on-chain approval

        _emit({"type": "result", "command": "approve-builder-fee", "status": "ok", "response": str(result)})
    except Exception as e:
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)


@app.command("check-builder-fee")
def check_builder_fee_cmd(
    user_address: str = typer.Argument(..., help="User wallet address to check"),
) -> None:
    """Check if a user has approved RIFT's builder fee."""
    from rift.builder_fee import check_builder_approval, BUILDER_ADDRESS

    result = check_builder_approval(user_address)
    _emit({"type": "result", "command": "check-builder-fee", "builder": BUILDER_ADDRESS, "user": user_address, **result})


@app.command("version")
def version() -> None:
    """Print engine version."""
    _emit({"type": "result", "command": "version", "version": __version__})


# ---------------------------------------------------------------------------
# Workbench commands — strategy config, code gen, quick test, experiments
# ---------------------------------------------------------------------------


@app.command("api-start")
def api_start(
    port: int = typer.Option(8420, "--port", help="Port to listen on"),
    require_auth: bool = typer.Option(False, "--require-auth", help="Require auth on all endpoints"),
    daemon: bool = typer.Option(False, "--daemon", help="Run as background daemon"),
) -> None:
    """Start the REST API server for dashboard and PMS integration."""
    import os as _os
    from rift.api import run_api, is_api_running

    if is_api_running():
        _emit({"type": "result", "command": "api-start", "status": "already_running"})
        return

    if daemon:
        pid = _os.fork()
        if pid > 0:
            _emit({"type": "result", "command": "api-start", "status": "started", "pid": pid, "port": port})
            return
        _os.setsid()
        devnull = _os.open(_os.devnull, _os.O_RDWR)
        _os.dup2(devnull, 0)
        _os.dup2(devnull, 1)
        _os.dup2(devnull, 2)
        _os.close(devnull)

    run_api(port=port, require_auth=require_auth)


@app.command("api-stop")
def api_stop() -> None:
    """Stop the REST API server."""
    from rift.api import stop_api
    result = stop_api()
    _emit({"type": "result", "command": "api-stop", **result})


