"""Portfolio Supervisor — manages multiple live trading daemons.

Reads portfolio.yaml, starts/stops strategy daemons, coordinates risk
across strategies, enforces scheduling, monitors health, fires alerts.

The supervisor itself is a daemon. It writes state to
~/.rift/algo/supervisor.json and a gate file (~/.rift/algo/gate.json)
that individual strategy daemons check before placing orders.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rift_trade.alerts import fire_alert, get_recent_alerts
from rift_trade.algo import (
    ALGO_DIR, ALGO_SESSIONS_DIR, ALGO_PIDS_DIR,
    _session_key, _session_state_file, _session_pid_file,
    _is_pid_alive, list_algo_sessions, stop_algo_session,
)

SUPERVISOR_STATE_FILE = ALGO_DIR / "supervisor.json"
SUPERVISOR_PID_FILE = ALGO_DIR / "supervisor.pid"
PORTFOLIO_CONFIG_FILE = ALGO_DIR / "portfolio.yaml"
GATE_FILE = ALGO_DIR / "gate.json"


def _emit(data: dict) -> None:
    """Write NDJSON to stdout."""
    print(json.dumps(data), flush=True)


class _PortfolioStrategy(__import__("pydantic").BaseModel):
    """Schema for one entry in portfolio.yaml `strategies:`."""
    name: str
    pair: str
    timeframe: str
    allocation: float

    # Pydantic v2 config — reject unknown fields so typos surface
    model_config = {"extra": "forbid"}


class _PortfolioRisk(__import__("pydantic").BaseModel):
    """Schema for the optional `risk:` block."""
    max_portfolio_drawdown: float | None = None
    max_strategy_drawdown: float | None = None
    kill_switch_drawdown: float | None = None
    max_gross_exposure: float | None = None

    model_config = {"extra": "forbid"}


class _PortfolioConfig(__import__("pydantic").BaseModel):
    """Top-level schema for portfolio.yaml."""
    initial_equity: float
    strategies: list[_PortfolioStrategy]
    risk: _PortfolioRisk | None = None
    alerts: list[dict] | None = None  # alerts schema is per-channel, lenient here

    model_config = {"extra": "forbid"}


def _load_portfolio_config(config_path: str = "") -> dict:
    """Load + validate portfolio.yaml configuration.

    Validates against `_PortfolioConfig` so malformed configs surface a
    field-level error instead of a deep stack trace mid-supervisor.
    """
    import yaml  # type: ignore
    from pydantic import ValidationError

    path = Path(config_path) if config_path else PORTFOLIO_CONFIG_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Portfolio config not found: {path}\n"
            f"  Create one at strategies/configs/portfolio.yaml (see strategies/configs/portfolio_btc.yaml for an example),\n"
            f"  then run: rift portfolio-start <path-to-config>"
        )

    raw = yaml.safe_load(path.read_text())
    if raw is None:
        raise ValueError(f"Portfolio config is empty: {path}")

    try:
        validated = _PortfolioConfig(**raw)
    except ValidationError as ve:
        # Build a human-friendly error that names the bad field + expected type
        problems = []
        for err in ve.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            problems.append(f"  - {loc}: {msg}")
        raise ValueError(
            f"Portfolio config invalid: {path}\n"
            + "\n".join(problems)
            + "\nFix the fields above and retry. See strategies/configs/portfolio_btc.yaml for the canonical shape."
        ) from None

    # Cross-field sanity: allocations should sum to ~1.0
    total_alloc = sum(s.allocation for s in validated.strategies)
    if not (0.99 <= total_alloc <= 1.01):
        raise ValueError(
            f"Portfolio config invalid: {path}\n"
            f"  strategies.allocation sums to {total_alloc:.4f}, expected ~1.0\n"
            f"  Adjust allocations so they total exactly 1.0 (e.g. 0.5 + 0.5, or 0.6 + 0.4)."
        )

    # Return the dict form (downstream code expects raw dicts, not pydantic models)
    return validated.model_dump(exclude_none=True)


def _write_gate(
    blocked: list[str],
    size_overrides: dict[str, float],
    paused: bool = False,
    reason: str | None = None,
    position_limits: dict[str, float] | None = None,
) -> None:
    """Write the gate file that strategy daemons read before placing orders."""
    ALGO_DIR.mkdir(parents=True, exist_ok=True)
    gate = {
        "blocked_strategies": blocked,
        "max_size_overrides": size_overrides,
        "max_position_usd": position_limits or {},
        "portfolio_paused": paused,
        "reason": reason,
        "updated_at": time.time(),
    }
    GATE_FILE.write_text(json.dumps(gate, indent=2))


def _clear_gate() -> None:
    """Remove gate file — no restrictions."""
    if GATE_FILE.exists():
        GATE_FILE.unlink()


def _read_session_state(key: str) -> dict | None:
    """Read a strategy daemon's state snapshot."""
    state_file = _session_state_file(key)
    if not state_file.exists():
        return None
    try:
        snapshot = json.loads(state_file.read_text())
        return snapshot.get("state", {})
    except Exception:
        return None


def _start_strategy_daemon(
    strategy: str,
    pair: str,
    private_key: str,
    account_address: str,
    equity: float = 0,
    interval: str = "1h",
) -> int | None:
    """Start a strategy daemon as a subprocess. Returns PID or None."""
    from rift_data.data import normalize_coin as _nc
    coin = _nc(pair)
    key = _session_key(strategy, coin)

    # Check if already running
    pid_file = _session_pid_file(key)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _is_pid_alive(pid):
                return pid  # already running
        except (ValueError, FileNotFoundError):
            pass

    # Find Python and engine dir.
    #
    # `rift_trade` is now a separate package from the meta `rift` CLI, so we
    # can't compute engine_dir from this file's path. Instead:
    #   - Use sys.executable for the Python binary — guaranteed to have every
    #     rift-* package installed since it's the interpreter we're running in.
    #   - Locate the engine directory via the `rift` package's __init__.py.
    import importlib.util
    _rift_spec = importlib.util.find_spec("rift")
    if _rift_spec and _rift_spec.origin:
        # rift/__init__.py → engine/src/rift/ → engine/src/ → engine/
        engine_dir = Path(_rift_spec.origin).parent.parent.parent
    else:
        # Fallback: walk up from this file. Works in dev layouts where
        # rift_trade lives under <repo>/packages/trade/src/rift_trade/.
        engine_dir = Path(__file__).parent.parent.parent.parent.parent / "engine"

    python = sys.executable  # same interpreter, so all packages are present
    strategies_dir = str(engine_dir.parent / "strategies")

    args = [
        python, "-m", "rift.cli", "algo",
        strategy,
        "--pair", pair,
        "--equity", str(equity),
        "--account", account_address,
        "--daemon",
        "--strategies-dir", strategies_dir,
    ]
    if interval != "1h":
        args.extend(["--tf", interval])

    env = {
        **os.environ,
        "HYPERLIQUID_PRIVATE_KEY": private_key,
        # No PYTHONPATH override needed — sys.executable already has the
        # rift packages installed via the engine venv.
        "PYTHONUNBUFFERED": "1",
    }

    # Start detached
    devnull = open(os.devnull, "r+")
    proc = subprocess.Popen(
        args,
        cwd=str(engine_dir),
        env=env,
        stdin=devnull,
        stdout=devnull,
        stderr=devnull,
        start_new_session=True,
    )
    devnull.close()

    return proc.pid


def _compute_portfolio_risk(session_states: dict[str, dict]) -> dict:
    """Compute aggregate portfolio risk metrics from all session states."""
    total_equity = 0.0
    net_exposure = 0.0
    gross_exposure = 0.0
    per_asset: dict[str, float] = {}
    peak_equity = 0.0

    for key, state in session_states.items():
        if not state:
            continue
        eq = state.get("total_equity", 0) or 0
        total_equity = max(total_equity, eq)  # use largest equity (shared account)
        peak_equity = max(peak_equity, state.get("peak_equity", 0) or 0)

        pos = state.get("position")
        if pos and total_equity > 0:
            pos_value = (pos.get("size", 0) or 0) * (pos.get("entry_price", 0) or 0)
            exposure_pct = pos_value / total_equity if total_equity > 0 else 0

            side_sign = 1.0 if pos.get("side") == "long" else -1.0
            net_exposure += exposure_pct * side_sign
            gross_exposure += exposure_pct

            # Per-asset tracking
            coin = key.split("_")[-1] if "_" in key else key
            per_asset[coin] = per_asset.get(coin, 0) + exposure_pct * side_sign

    initial = next(
        (s.get("initial_equity", 0) for s in session_states.values() if s),
        total_equity,
    )
    drawdown = 0.0
    if peak_equity > 0 and total_equity > 0:
        drawdown = (peak_equity - total_equity) / peak_equity

    return {
        "total_equity": round(total_equity, 2),
        "net_exposure": round(net_exposure, 4),
        "gross_exposure": round(gross_exposure, 4),
        "per_asset": {k: round(v, 4) for k, v in per_asset.items()},
        "drawdown_from_peak": round(drawdown, 4),
        "peak_equity": round(peak_equity, 2),
        "initial_equity": round(initial, 2) if initial else 0,
    }


def _check_schedule(schedule) -> bool:
    """Check if a strategy should be running right now based on its schedule."""
    if schedule is None or schedule == "always":
        return True

    if isinstance(schedule, dict):
        start_str = schedule.get("start", "00:00 UTC")
        stop_str = schedule.get("stop", "23:59 UTC")

        # Parse HH:MM from strings like "00:00 UTC"
        try:
            start_h, start_m = map(int, start_str.split(" ")[0].split(":"))
            stop_h, stop_m = map(int, stop_str.split(" ")[0].split(":"))
        except (ValueError, IndexError):
            return True  # Can't parse = always on

        now = datetime.now(timezone.utc)
        now_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        stop_minutes = stop_h * 60 + stop_m

        if start_minutes <= stop_minutes:
            return start_minutes <= now_minutes < stop_minutes
        else:
            # Wraps midnight (e.g., 22:00 - 06:00)
            return now_minutes >= start_minutes or now_minutes < stop_minutes

    return True


def _save_supervisor_state(
    config: dict,
    strategy_statuses: list[dict],
    portfolio_risk: dict,
    started_at: str,
) -> None:
    """Write supervisor state file for viewers and MCP."""
    ALGO_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "name": config.get("name", "portfolio"),
        "running": True,
        "pid": os.getpid(),
        "strategies": strategy_statuses,
        "portfolio": portfolio_risk,
        "started_at": started_at,
        "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "alerts_fired": len(get_recent_alerts(limit=1000)),
    }
    SUPERVISOR_STATE_FILE.write_text(json.dumps(state, indent=2))


def run_supervisor(
    config_path: str = "",
    private_key: str = "",
    account_address: str = "",
) -> None:
    """Run the portfolio supervisor daemon."""
    config = _load_portfolio_config(config_path)
    strategies_config = config.get("strategies", [])
    risk_config = config.get("risk", {})
    rotation_config = config.get("rotation", {})
    alert_configs = config.get("alerts", [{"type": "log", "events": ["all"]}])

    max_net = risk_config.get("max_net_exposure", 1.0)
    max_gross = risk_config.get("max_gross_exposure", 1.5)
    max_per_asset = risk_config.get("max_per_asset", 0.8)
    max_correlation = risk_config.get("max_correlation", 0.85)
    max_drawdown = risk_config.get("max_drawdown", 0.15)

    rotation_enabled = rotation_config.get("enabled", True)
    pause_grade = rotation_config.get("pause_grade", "D")
    stop_grade = rotation_config.get("stop_grade", "F")
    grade_order = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}

    # Reports — defaulted to OFF, user must explicitly enable
    reports_config = config.get("reports", {})
    reports_daily = reports_config.get("daily", False)
    reports_weekly = reports_config.get("weekly", False)
    reports_output_dir = reports_config.get("output_dir", "")
    last_daily_report_day = ""
    last_weekly_report_week = ""

    # Adaptive optimization — defaulted to OFF
    opt_config = config.get("optimization", {})
    opt_enabled = opt_config.get("enabled", False)
    opt_train_months = opt_config.get("train_months", 6)
    opt_trials = opt_config.get("trials", 40)
    opt_sharpe_double = opt_config.get("sharpe_thresholds", {}).get("double", 3.0)
    opt_sharpe_normal = opt_config.get("sharpe_thresholds", {}).get("normal", 2.0)
    opt_sharpe_half = opt_config.get("sharpe_thresholds", {}).get("half", 1.0)
    last_weekly_optimize_week = ""
    train_sharpes: dict[str, float] = {}  # strategy_key → last train sharpe

    # Use provided credentials or load from config
    if not private_key:
        from rift_core.config import get_env_var
        private_key = get_env_var("HYPERLIQUID_PRIVATE_KEY")
    if not account_address:
        account_address = config.get("account", "")

    if not private_key or not account_address:
        _emit({"type": "error", "msg": "No credentials. Set HYPERLIQUID_PRIVATE_KEY or configure account in portfolio.yaml"})
        sys.exit(1)

    # Write PID file
    ALGO_DIR.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_PID_FILE.write_text(str(os.getpid()))

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    health_grades: dict[str, str] = {}  # key -> last known grade
    paused_strategies: set[str] = set()

    # Graceful shutdown
    running = True

    def handle_shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    _emit({"type": "status", "msg": f"Portfolio supervisor started — managing {len(strategies_config)} strategies"})

    while running:
        try:
            blocked_strategies: list[str] = []
            size_overrides: dict[str, float] = {}
            position_limits: dict[str, float] = {}
            session_states: dict[str, dict] = {}
            strategy_statuses: list[dict] = []

            # ─── MANAGE EACH STRATEGY ───
            for strat_config in strategies_config:
                name = strat_config.get("name", "")
                pair = strat_config.get("pair", "BTC")
                enabled = strat_config.get("enabled", True)
                schedule = strat_config.get("schedule", "always")
                max_alloc = strat_config.get("max_allocation", 1.0)
                max_pos_usd = strat_config.get("max_position_usd")
                interval = strat_config.get("interval", "1h")
                strat_account = strat_config.get("account", "")

                from rift_data.data import normalize_coin as _nc
                coin = _nc(pair)
                key = _session_key(name, coin)
                should_run = enabled and _check_schedule(schedule) and key not in paused_strategies

                # Check if daemon is running
                pid_file = _session_pid_file(key)
                daemon_running = False
                daemon_pid = None
                if pid_file.exists():
                    try:
                        daemon_pid = int(pid_file.read_text().strip())
                        daemon_running = _is_pid_alive(daemon_pid)
                    except (ValueError, FileNotFoundError):
                        pass

                # Start/stop as needed
                if should_run and not daemon_running:
                    # Use strategy-specific account or default
                    acct = strat_account or account_address
                    pk = private_key  # TODO: multi-account credential lookup

                    pid = _start_strategy_daemon(
                        name, pair, pk, acct,
                        equity=0, interval=interval,
                    )
                    if pid:
                        daemon_pid = pid
                        daemon_running = True
                        fire_alert("schedule_start", {"strategy": name, "pair": coin, "pid": pid}, alert_configs)

                elif not should_run and daemon_running:
                    stop_algo_session(name, pair)
                    daemon_running = False
                    if not _check_schedule(schedule):
                        fire_alert("schedule_stop", {"strategy": name, "pair": coin}, alert_configs)

                # Read state
                state = _read_session_state(key) if daemon_running else None
                if state:
                    session_states[key] = state

                # Apply allocation limits
                if max_alloc < 1.0:
                    size_overrides[key] = max_alloc
                if max_pos_usd:
                    position_limits[key] = max_pos_usd

                # Health rotation
                if rotation_enabled and state and daemon_running:
                    health_grade = state.get("health_grade")
                    if health_grade:
                        old_grade = health_grades.get(key)
                        if old_grade and old_grade != health_grade:
                            old_rank = grade_order.get(old_grade, 3)
                            new_rank = grade_order.get(health_grade, 3)
                            if new_rank < old_rank:
                                fire_alert("health_drop", {
                                    "strategy": name, "pair": coin,
                                    "old_grade": old_grade, "new_grade": health_grade,
                                    "score": state.get("health_score", 0),
                                }, alert_configs)

                        health_grades[key] = health_grade

                        if grade_order.get(health_grade, 3) <= grade_order.get(stop_grade, 1):
                            # Auto-stop
                            stop_algo_session(name, pair)
                            paused_strategies.add(key)
                            daemon_running = False
                            fire_alert("health_rotation", {
                                "strategy": name, "pair": coin,
                                "action": "stopped", "grade": health_grade,
                            }, alert_configs)
                        elif grade_order.get(health_grade, 3) <= grade_order.get(pause_grade, 2):
                            # Reduce size
                            size_overrides[key] = max_alloc * 0.5
                            fire_alert("health_rotation", {
                                "strategy": name, "pair": coin,
                                "action": "reduced", "grade": health_grade,
                            }, alert_configs)

                # Check if daemon died unexpectedly
                if should_run and not daemon_running and daemon_pid and not _is_pid_alive(daemon_pid or 0):
                    if daemon_pid:
                        fire_alert("session_died", {
                            "strategy": name, "pair": coin, "pid": daemon_pid,
                        }, alert_configs)

                # Build status entry
                status_entry: dict = {
                    "name": name,
                    "pair": coin,
                    "status": "running" if daemon_running else ("paused" if key in paused_strategies else ("scheduled_off" if not _check_schedule(schedule) else "stopped")),
                    "pid": daemon_pid if daemon_running else None,
                    "allocation": max_alloc,
                }
                if state:
                    status_entry.update({
                        "equity": state.get("total_equity", 0),
                        "pnl_pct": state.get("total_pnl_pct", 0),
                        "num_trades": state.get("num_trades", 0),
                        "health_grade": state.get("health_grade"),
                        "health_score": state.get("health_score"),
                        "position": state.get("position"),
                    })
                if schedule != "always":
                    status_entry["schedule"] = schedule
                strategy_statuses.append(status_entry)

            # ─── PORTFOLIO-LEVEL RISK ───
            portfolio_risk = _compute_portfolio_risk(session_states)

            # Check risk limits
            portfolio_paused = False
            if abs(portfolio_risk["net_exposure"]) > max_net:
                # Block all new entries
                for key in session_states:
                    if key not in blocked_strategies:
                        blocked_strategies.append(key)
                fire_alert("risk_blocked", {
                    "reason": f"Net exposure {portfolio_risk['net_exposure']:.0%} > {max_net:.0%}",
                }, alert_configs)

            if portfolio_risk["gross_exposure"] > max_gross:
                for key in session_states:
                    if key not in blocked_strategies:
                        blocked_strategies.append(key)

            for asset, exposure in portfolio_risk["per_asset"].items():
                if abs(exposure) > max_per_asset:
                    for key in session_states:
                        if asset in key and key not in blocked_strategies:
                            blocked_strategies.append(key)

            # Correlation guard
            corr_matrix: dict[str, dict[str, float]] = {}
            keys_with_prices = {k: s.get("price_history", []) for k, s in session_states.items() if s and len(s.get("price_history", [])) >= 5}
            key_list = list(keys_with_prices.keys())
            if len(key_list) >= 2:
                import numpy as _np
                for i in range(len(key_list)):
                    corr_matrix[key_list[i]] = {}
                    for j in range(i + 1, len(key_list)):
                        p1 = _np.array(keys_with_prices[key_list[i]][-20:], dtype=float)
                        p2 = _np.array(keys_with_prices[key_list[j]][-20:], dtype=float)
                        min_len = min(len(p1), len(p2))
                        if min_len >= 5:
                            r1 = _np.diff(p1[:min_len]) / p1[:min_len-1]
                            r2 = _np.diff(p2[:min_len]) / p2[:min_len-1]
                            mask = _np.isfinite(r1) & _np.isfinite(r2)
                            if mask.sum() >= 3:
                                corr = float(_np.corrcoef(r1[mask], r2[mask])[0, 1])
                                if not _np.isnan(corr):
                                    corr_matrix[key_list[i]][key_list[j]] = round(corr, 3)
                                    if abs(corr) > max_correlation:
                                        # Block the newer strategy
                                        newer = key_list[j]
                                        if newer not in blocked_strategies:
                                            blocked_strategies.append(newer)
                                        fire_alert("risk_blocked", {
                                            "reason": f"correlation with {key_list[i]} at {corr:.0%}",
                                            "strategy": newer,
                                        }, alert_configs)
            portfolio_risk["correlation_matrix"] = corr_matrix

            # Drawdown checks
            dd = portfolio_risk["drawdown_from_peak"]
            if dd > max_drawdown:
                # Kill switch — stop everything
                portfolio_paused = True
                fire_alert("drawdown_kill", {
                    "drawdown_pct": dd * 100, "limit_pct": max_drawdown * 100,
                }, alert_configs)
                for strat_config in strategies_config:
                    stop_algo_session(strat_config["name"], strat_config.get("pair", "BTC"))
                running = False
            elif dd > max_drawdown * 0.5:
                fire_alert("drawdown_warning", {
                    "drawdown_pct": dd * 100, "limit_pct": max_drawdown * 100,
                }, alert_configs)

            # Write gate file
            _write_gate(blocked_strategies, size_overrides, portfolio_paused, position_limits=position_limits)

            # Write supervisor state
            _save_supervisor_state(config, strategy_statuses, portfolio_risk, started_at)

            # ─── SCHEDULED REPORTS (opt-in) ───
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            this_week = now.strftime("%Y-W%W")

            if reports_daily and today != last_daily_report_day:
                try:
                    from rift.reports import generate_portfolio_report
                    path = generate_portfolio_report(output_dir=reports_output_dir, period="daily")
                    if path:
                        last_daily_report_day = today
                        fire_alert("report_generated", {"type": "daily", "path": path}, alert_configs)
                except Exception:
                    pass

            if reports_weekly and this_week != last_weekly_report_week and now.weekday() == 0:
                try:
                    from rift.reports import generate_portfolio_report
                    path = generate_portfolio_report(output_dir=reports_output_dir, period="weekly")
                    if path:
                        last_weekly_report_week = this_week
                        fire_alert("report_generated", {"type": "weekly", "path": path}, alert_configs)
                except Exception:
                    pass

            # ─── WEEKLY ADAPTIVE OPTIMIZATION (opt-in) ───
            if opt_enabled and this_week != last_weekly_optimize_week and now.weekday() == 0:
                try:
                    import polars as _pl
                    from rift_engine.smart_optimize import smart_sweep
                    from rift_engine.strategy import get_strategy

                    _emit({"type": "status", "msg": "Weekly optimization starting..."})
                    data_dir = Path(__file__).parent.parent.parent.parent.parent / "packages" / "cli" / "data"

                    for strat_config in strategies_config:
                        name = strat_config.get("name", "")
                        pair = strat_config.get("pair", "BTC")
                        from rift_data.data import normalize_coin as _nc
                        coin = _nc(pair)
                        key = _session_key(name, coin)
                        interval = strat_config.get("interval", "1h")

                        candle_file = data_dir / coin / f"candles_{interval}.parquet"
                        funding_file = data_dir / coin / "funding_hourly.parquet"
                        if not candle_file.exists() or not funding_file.exists():
                            continue

                        try:
                            strat_cls = get_strategy(name)
                            df = _pl.read_parquet(candle_file)
                            fdf = _pl.read_parquet(funding_file)

                            # Use trailing N months
                            train_ms = opt_train_months * 30 * 24 * 3600 * 1000
                            cutoff = int(time.time() * 1000) - train_ms
                            df = df.filter(_pl.col("timestamp") >= cutoff)
                            fdf = fdf.filter(_pl.col("timestamp") >= cutoff)

                            if len(df) < 500:
                                continue

                            # Get param ranges from strategy config
                            import dataclasses as _dc
                            param_ranges = {}
                            for f in _dc.fields(strat_cls.config_class):
                                val = f.default
                                if isinstance(val, float) and val > 0:
                                    param_ranges[f.name] = (val * 0.5, val * 2.0, val * 0.1)
                                elif isinstance(val, int) and val > 1 and f.name not in ("bb_period", "keltner_period", "keltner_atr_period", "rsi_period", "vwap_period"):
                                    param_ranges[f.name] = (max(1, val // 2), val * 2, max(1, val // 4))

                            if not param_ranges:
                                continue

                            result = smart_sweep(
                                strategy_cls=type(strat_cls()),
                                df=df, param_ranges=param_ranges,
                                funding_df=fdf, pair=coin, interval=interval,
                                n_trials=opt_trials, optimize_target="sharpe",
                            )

                            train_sharpes[key] = result.best_sharpe

                            # Apply sizing based on train Sharpe
                            if result.best_sharpe >= opt_sharpe_double:
                                size_overrides[key] = 2.0
                            elif result.best_sharpe >= opt_sharpe_normal:
                                size_overrides[key] = 1.0
                            elif result.best_sharpe >= opt_sharpe_half:
                                size_overrides[key] = 0.5
                            else:
                                if key not in blocked_strategies:
                                    blocked_strategies.append(key)

                            fire_alert("optimization", {
                                "strategy": name, "pair": coin,
                                "train_sharpe": round(result.best_sharpe, 2),
                                "sizing": size_overrides.get(key, 0),
                                "best_return": round(result.best_return, 2),
                            }, alert_configs)

                        except Exception as e:
                            _emit({"type": "status", "msg": f"Optimization error for {name}: {e}"})

                    last_weekly_optimize_week = this_week
                    # Re-write gate with updated sizing
                    _write_gate(blocked_strategies, size_overrides, portfolio_paused, position_limits=position_limits)
                    _emit({"type": "status", "msg": f"Weekly optimization complete. Sharpes: {train_sharpes}"})

                except Exception as e:
                    _emit({"type": "status", "msg": f"Weekly optimization failed: {e}"})

        except Exception as e:
            _emit({"type": "error", "msg": f"Supervisor error: {e}"})

        time.sleep(5)

    # ─── SHUTDOWN ───
    _emit({"type": "status", "msg": "Supervisor shutting down..."})

    # Stop all strategy daemons
    for strat_config in strategies_config:
        name = strat_config.get("name", "")
        pair = strat_config.get("pair", "BTC")
        from rift_data.data import normalize_coin as _nc
        coin = _nc(pair)
        key = _session_key(name, coin)
        pid_file = _session_pid_file(key)
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _is_pid_alive(pid):
                    os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    # Wait for all to exit
    time.sleep(5)

    # Clean up
    _clear_gate()
    if SUPERVISOR_PID_FILE.exists():
        SUPERVISOR_PID_FILE.unlink()

    # Write final state
    if SUPERVISOR_STATE_FILE.exists():
        try:
            state = json.loads(SUPERVISOR_STATE_FILE.read_text())
            state["running"] = False
            SUPERVISOR_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    _emit({"type": "result", "command": "portfolio-stop", "status": "stopped"})


def get_supervisor_status() -> dict | None:
    """Read supervisor state (for CLI/MCP)."""
    if not SUPERVISOR_STATE_FILE.exists():
        return None
    try:
        state = json.loads(SUPERVISOR_STATE_FILE.read_text())
        # Verify supervisor is actually running
        pid = state.get("pid")
        if pid and not _is_pid_alive(pid):
            state["running"] = False
        return state
    except Exception:
        return None


def is_supervisor_running() -> bool:
    """Check if portfolio supervisor is running."""
    if not SUPERVISOR_PID_FILE.exists():
        return False
    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
        return _is_pid_alive(pid)
    except (ValueError, FileNotFoundError):
        return False


def stop_supervisor() -> dict:
    """Stop the portfolio supervisor."""
    if not SUPERVISOR_PID_FILE.exists():
        return {"status": "not_running"}

    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
    except (ValueError, FileNotFoundError):
        return {"status": "error", "msg": "Invalid PID file"}

    if not _is_pid_alive(pid):
        SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        return {"status": "not_running"}

    os.kill(pid, signal.SIGTERM)

    # Wait for shutdown
    for _ in range(60):
        if not _is_pid_alive(pid):
            break
        time.sleep(0.5)

    # Read final state
    final = get_supervisor_status()
    SUPERVISOR_PID_FILE.unlink(missing_ok=True)

    return {"status": "stopped", "final_state": final}
