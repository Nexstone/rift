"""Research pipeline — full strategy validation in one call.

Chains: backtest → walk-forward → Monte Carlo → multi-pair test
Returns a graded result (A/B/C/D/F) with next-step recommendation.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rift_engine.backtest import run_backtest
from rift_engine.walkforward import run_walk_forward, parse_walk_forward_config
from rift_engine.montecarlo import run_montecarlo
from rift_data.data import load_candles, load_funding_rates, fetch_candles, save_candles, fetch_funding_rates, save_funding_rates, get_info_client
from rift_data.historical import load_candles_smart, load_funding_smart
from rift_engine.strategy import discover_strategies, get_strategy


def _emit(data: dict) -> None:
    import math
    def sanitize(obj):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        elif isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize(v) for v in obj]
        return obj
    print(json.dumps(sanitize(data)), flush=True)


def _load_oi_data(pair: str) -> 'pl.DataFrame | None':
    """Load OI data, preferring local cache and falling back to bundled.

    OI is supplementary — strategies that don't consume it run fine without it.
    Returns None if neither cache nor bundled data is available.
    """
    try:
        from rift_data.coinalyze import load_hl_historical_oi
        from rift_data.data import normalize_coin
        return load_hl_historical_oi(normalize_coin(pair))
    except Exception:
        return None


def _ensure_data(pair: str, interval: str, emit_progress: bool = True) -> tuple:
    """Load data, auto-fetching if not cached. Returns (candle_df, funding_df, oi_df).

    Data fallback chain: bundled (shipped with RIFT) → Hyperliquid cache → live fetch.
    This gives users up to 2.5 years of data out of the box without any fetch step.
    """
    # Try smart loader first (merges bundled + Hyperliquid cache)
    df = load_candles_smart(pair, interval)

    if df is None or len(df) == 0:
        # Last resort: live fetch from Hyperliquid
        if emit_progress:
            _emit({"type": "progress", "pct": 0, "msg": f"Fetching {pair} {interval} data..."})
        try:
            df = fetch_candles(pair, interval)
            if len(df) > 0:
                save_candles(df, pair, interval)
        except Exception:
            return None, None, None

    # Funding: smart loader merges bundled + Hyperliquid
    funding_df = load_funding_smart(pair)

    if funding_df is None or len(funding_df) == 0:
        try:
            if df is not None and len(df) > 0:
                funding_df = fetch_funding_rates(pair, start_time=int(df["timestamp"].min()))
                if funding_df is not None and len(funding_df) > 0:
                    save_funding_rates(funding_df, pair)
        except Exception:
            pass

    # OI data
    oi_df = _load_oi_data(pair)

    return df, funding_df, oi_df


def run_research_pipeline(
    strategy_name: str,
    pair: str,
    interval: str = "",
    initial_equity: float = 10000.0,
    strategies_dir: str = "",
    multi_pair_count: int = 3,
    config_overrides: dict | None = None,
) -> dict:
    """Run the full research pipeline and return graded results.

    Args:
        config_overrides: Optional dict of config field overrides.
            If provided, the strategy is instantiated with these values
            instead of defaults. Used by the optimizer to validate
            sweep results without creating a new strategy file.
    """

    # Discover strategies from all sources
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    from rift_engine.workbench import GENERATED_DIR
    if GENERATED_DIR.exists():
        dirs.append(GENERATED_DIR)
    discover_strategies(dirs)

    # Load strategy
    try:
        strategy_cls = get_strategy(strategy_name)
    except KeyError as e:
        return {"error": str(e).strip('"')}

    # Use strategy's default interval if not specified
    if not interval:
        interval = strategy_cls.default_interval

    # Build config override factory — creates strategy with custom params
    def _make_strategy():
        if config_overrides and strategy_cls.config_class:
            import dataclasses
            defaults = {}
            for f in dataclasses.fields(strategy_cls.config_class):
                if f.default is not dataclasses.MISSING:
                    defaults[f.name] = f.default
                elif f.default_factory is not dataclasses.MISSING:
                    defaults[f.name] = f.default_factory()
            defaults.update(config_overrides)
            config = strategy_cls.config_class(**defaults)
            return strategy_cls(config=config)
        return strategy_cls()

    override_label = ""
    if config_overrides:
        override_label = " (optimized)"
        params_str = ", ".join(f"{k}={v}" for k, v in config_overrides.items())
        _emit({"type": "progress", "pct": 3, "msg": f"Config overrides: {params_str}"})

    _emit({"type": "progress", "pct": 5, "msg": f"Strategy: {strategy_name}{override_label} | Pair: {pair} | Timeframe: {interval}"})

    # Step 1: Ensure data
    _emit({"type": "step", "step": 1, "name": "data", "msg": "Loading data..."})
    df, funding_df, oi_df = _ensure_data(pair, interval)
    if df is None or len(df) == 0:
        return {"error": f"Could not fetch data for {pair} {interval}"}

    candle_count = len(df)
    funding_count = len(funding_df) if funding_df is not None else 0
    _emit({"type": "step_done", "step": 1, "msg": f"Loaded {candle_count} candles + {funding_count} funding rates"})

    # Step 2: Backtest
    _emit({"type": "step", "step": 2, "name": "backtest", "msg": "Running backtest..."})
    strategy = _make_strategy()
    bt = run_backtest(
        strategy=strategy, df=df, strategy_name=strategy_name,
        pair=pair, interval=interval, initial_equity=initial_equity,
        funding_df=funding_df, oi_df=oi_df, silent=True,
    )
    bt_result = {
        "return_pct": round(bt.total_return_pct, 2),
        "sharpe": round(bt.sharpe_ratio, 4),
        "profit_factor": round(bt.profit_factor, 2),
        "max_drawdown_pct": round(bt.max_drawdown_pct, 2),
        "win_rate": round(bt.win_rate, 2),
        "num_trades": bt.num_trades,
        "total_funding": round(bt.total_funding, 2),
    }
    _emit({"type": "step_done", "step": 2, "msg": f"Return: {bt_result['return_pct']}%, Sharpe: {bt_result['sharpe']}, Trades: {bt_result['num_trades']}"})

    if bt.num_trades == 0:
        return {
            "strategy": strategy_name, "pair": pair, "interval": interval,
            "backtest": bt_result, "grade": "F",
            "verdict": "Strategy made zero trades on this data.",
            "next_step": "Try a different pair or adjust strategy parameters.",
        }

    # Step 3: Walk-Forward
    _emit({"type": "step", "step": 3, "name": "walkforward", "msg": "Running walk-forward analysis..."})
    wf_result = {}
    try:
        # For walk-forward, we need a factory that creates fresh instances with overrides
        wf_cls = strategy_cls
        if config_overrides and strategy_cls.config_class:
            import dataclasses as _dc
            _defaults = {}
            for f in _dc.fields(strategy_cls.config_class):
                if f.default is not _dc.MISSING:
                    _defaults[f.name] = f.default
                elif f.default_factory is not _dc.MISSING:
                    _defaults[f.name] = f.default_factory()
            _defaults.update(config_overrides)
            _override_config = strategy_cls.config_class(**_defaults)

            class _OverriddenStrategy(strategy_cls):
                def __init__(self, config=None):
                    super().__init__(config=_override_config)
            _OverriddenStrategy.config_class = strategy_cls.config_class
            _OverriddenStrategy.default_interval = strategy_cls.default_interval
            wf_cls = _OverriddenStrategy

        # Walk-forward window sizing — read directly from the strategy's
        # declared `recommended_train_months` / `recommended_test_months`
        # class attributes. Defaults (2 / 1) live on the base `Strategy` class.
        # ML / HMM / long-warmup strategies should override these explicitly.
        _test_strat = _make_strategy()
        _wf_train = getattr(_test_strat, 'recommended_train_months', 2)
        _wf_test = getattr(_test_strat, 'recommended_test_months', 1)

        wf = run_walk_forward(
            strategy=_test_strat, df=df, strategy_name=strategy_name,
            pair=pair, interval=interval, initial_equity=initial_equity,
            funding_df=funding_df, strategy_cls=wf_cls,
            train_months=_wf_train, test_months=_wf_test,
            oi_df=oi_df,
        )
        wf_result = {
            "degradation_ratio": round(wf.degradation_ratio, 4),
            "profitable_windows": round(wf.pct_profitable_windows, 1),
            "combined_oos_return": round(wf.combined_oos_return, 2),
            "num_windows": wf.num_windows,
            "oos_avg_sharpe": round(wf.avg_oos_sharpe, 4),
        }
        grade_label = "ROBUST" if wf.degradation_ratio >= 0.7 else "MODERATE" if wf.degradation_ratio >= 0.4 else "WEAK" if wf.degradation_ratio > 0 else "OVERFIT"
        _emit({"type": "step_done", "step": 3, "msg": f"Degradation: {wf_result['degradation_ratio']} ({grade_label}), {wf_result['profitable_windows']}% profitable windows"})
    except Exception as e:
        wf_result = {"error": str(e), "degradation_ratio": 0, "profitable_windows": 0}
        _emit({"type": "step_done", "step": 3, "msg": f"Walk-forward failed: {e}"})

    # Step 4: Monte Carlo
    _emit({"type": "step", "step": 4, "name": "montecarlo", "msg": "Running Monte Carlo simulation (10,000 paths)..."})
    mc_result = {}
    try:
        mc = run_montecarlo(bt, num_simulations=10000)
        mc_result = {
            "prob_profit": round(mc.prob_profit, 2),
            "prob_ruin": round(mc.prob_ruin, 2),
            "p5": round(mc.p5, 2),
            "p50": round(mc.p50, 2),
            "p95": round(mc.p95, 2),
        }
        _emit({"type": "step_done", "step": 4, "msg": f"Profit probability: {mc_result['prob_profit']}%, Ruin: {mc_result['prob_ruin']}%"})
    except Exception as e:
        mc_result = {"error": str(e), "prob_profit": 0, "prob_ruin": 100}
        _emit({"type": "step_done", "step": 4, "msg": f"Monte Carlo failed: {e}"})

    # Step 5: Multi-pair test
    _emit({"type": "step", "step": 5, "name": "multi_pair", "msg": f"Testing across {multi_pair_count} additional pairs..."})
    multi_results = []
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

        # Pick top pairs excluding the primary pair
        from rift_data.data import normalize_coin
        coin = normalize_coin(pair)
        test_pairs = [p for p, _ in volume_pairs if p != coin][:multi_pair_count]

        for tp in test_pairs:
            try:
                tp_df, tp_funding, tp_oi = _ensure_data(tp, interval, emit_progress=False)
                if tp_df is None or len(tp_df) < 100:
                    continue
                tp_strat = _make_strategy()
                tp_bt = run_backtest(
                    strategy=tp_strat, df=tp_df, strategy_name=strategy_name,
                    pair=tp, interval=interval, initial_equity=initial_equity,
                    funding_df=tp_funding, oi_df=tp_oi, silent=True,
                )
                multi_results.append({
                    "pair": tp,
                    "return_pct": round(tp_bt.total_return_pct, 2),
                    "sharpe": round(tp_bt.sharpe_ratio, 4),
                    "num_trades": tp_bt.num_trades,
                })
            except Exception:
                continue

        profitable_pairs = sum(1 for r in multi_results if r["return_pct"] > 0)
        total_tested = len(multi_results)
        _emit({"type": "step_done", "step": 5, "msg": f"Profitable on {profitable_pairs}/{total_tested} additional pairs"})
    except Exception as e:
        _emit({"type": "step_done", "step": 5, "msg": f"Multi-pair test failed: {e}"})

    # Step 6: Feature Importance (XGBoost)
    _emit({"type": "step", "step": 6, "name": "features", "msg": "Analyzing predictive features..."})
    feature_result = {}
    try:
        from rift_engine.smart_optimize import feature_importance
        fi = feature_importance(strategy_cls, df, funding_df, oi_df, pair, interval)
        if fi:
            top_features = list(fi.items())[:5]
            feature_result = {
                "top_features": {name: round(imp, 4) for name, imp in top_features},
                "strongest": top_features[0][0] if top_features else "unknown",
            }
            top_str = ", ".join(f"{name} ({imp:.0%})" for name, imp in top_features[:3])
            _emit({"type": "step_done", "step": 6, "msg": f"Top predictors: {top_str}"})
        else:
            _emit({"type": "step_done", "step": 6, "msg": "Insufficient trades for feature analysis"})
    except Exception as e:
        _emit({"type": "step_done", "step": 6, "msg": f"Feature analysis skipped: {e}"})

    # Step 7: Volatility Forecast (GARCH)
    _emit({"type": "step", "step": 7, "name": "volatility", "msg": "Forecasting volatility..."})
    vol_result = {}
    try:
        from rift_research.reports import forecast_volatility
        from rift_substrate import periods_per_year_for_interval
        closes = df["close"].to_list()
        vol = forecast_volatility(closes, periods_per_year=periods_per_year_for_interval(interval))
        vol_result = vol
        vol_status = "expanding" if vol.get("vol_expanding") else "stable"
        _emit({"type": "step_done", "step": 7, "msg": f"Volatility forecast: {vol_status} ({vol.get('current_vol', 0):.1%} → {vol.get('forecast_vol', 0):.1%})"})
    except Exception as e:
        _emit({"type": "step_done", "step": 7, "msg": f"Volatility forecast skipped: {e}"})

    # Step 8: Health Check + Tearsheet
    _emit({"type": "step", "step": 8, "name": "health", "msg": "Running health check & generating report..."})
    health_result = {}
    tearsheet_path = ""
    try:
        from rift_trade.health import run_health_check
        import numpy as _np

        if bt.num_trades >= 10:
            split = int(len(bt.trades) * 0.7)
            baseline_trades = bt.trades[:split]
            recent_trades = bt.trades[split:]
            closes_arr = df["close"].to_numpy().astype(float)
            mkt_returns = list(_np.diff(closes_arr) / closes_arr[:-1])
            baseline_wr = len([t for t in baseline_trades if t.pnl > 0]) / len(baseline_trades) * 100 if baseline_trades else 50

            health = run_health_check(
                recent_trades=recent_trades,
                baseline_trades=baseline_trades,
                equity_curve=bt.equity_curve,
                market_returns=mkt_returns,
                baseline_win_rate=baseline_wr,
            )
            health_result = health.to_dict()
            _emit({"type": "step_done", "step": 8, "msg": f"Health: {health.score}/100 ({health.grade})"})
        else:
            _emit({"type": "step_done", "step": 8, "msg": "Insufficient trades for health analysis"})
    except Exception as e:
        _emit({"type": "step_done", "step": 8, "msg": f"Health check skipped: {e}"})

    # Generate tearsheet (non-blocking, save to file)
    try:
        from rift_research.reports import generate_tearsheet
        tearsheet_path = generate_tearsheet(bt.equity_curve, f"{strategy_name}_{pair}_{interval}")
    except Exception:
        pass

    # ─── Advanced validations (substrate-driven) ──────────────────
    # Strategy-agnostic: runs the same suite on every strategy via class-level
    # `promotion_gates` config (or industry defaults if the strategy is silent).
    advanced_sections: dict = {}
    try:
        from rift_research.advanced import run_advanced_validations
        advanced_sections = run_advanced_validations(
            bt=bt,
            df=df,
            strategy_cls=strategy_cls,
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            multi_pair_results=multi_results,
            config_overrides=config_overrides,
            seed=None,
            emit_fn=_emit,
        )
    except Exception as exc:
        advanced_sections = {"error": str(exc)}

    # Compute grade
    grade, verdict, next_step = _compute_grade(bt_result, wf_result, mc_result, multi_results)

    return {
        "strategy": strategy_name,
        "pair": pair,
        "interval": interval,
        "backtest": bt_result,
        "walkforward": wf_result,
        "montecarlo": mc_result,
        "multi_pair": multi_results,
        "features": feature_result,
        "volatility": vol_result,
        "health": health_result,
        "tearsheet": tearsheet_path,
        "grade": grade,
        "verdict": verdict,
        "next_step": next_step,
        # ─── Advanced validations (strategy-agnostic substrate wiring) ─
        "purged_cv": advanced_sections.get("purged_cv", {"status": "skipped"}),
        "alpha_decay": advanced_sections.get("alpha_decay", {"status": "skipped"}),
        "capacity": advanced_sections.get("capacity", {"status": "skipped"}),
        "cross_impact": advanced_sections.get("cross_impact", {"status": "skipped"}),
        "promotion_verdict": advanced_sections.get("promotion_verdict", {"status": "skipped"}),
        "sealed_bundle": advanced_sections.get("sealed_bundle", {"status": "skipped"}),
    }


def _compute_grade(bt, wf, mc, multi) -> tuple[str, str, str]:
    """Compute A-F grade from validation results."""
    sharpe = bt.get("sharpe", 0)
    ret = bt.get("return_pct", 0)
    dd = bt.get("max_drawdown_pct", 0)
    trades = bt.get("num_trades", 0)

    wf_deg = wf.get("degradation_ratio", 0)
    wf_profitable = wf.get("profitable_windows", 0)

    mc_profit = mc.get("prob_profit", 0)
    mc_ruin = mc.get("prob_ruin", 100)
    mc_p5 = mc.get("p5", -100)

    multi_profitable = sum(1 for r in multi if r.get("return_pct", 0) > 0) if multi else 0
    multi_total = len(multi) if multi else 0

    # Grade A: Everything excellent
    if (wf_deg >= 0.7 and mc_profit >= 90 and dd > -15 and ret > 10
            and mc_ruin == 0 and sharpe > 1.0):
        return (
            "A",
            "Strategy is validated and robust. Strong edge confirmed across all tests.",
            f"Ready to go live. Run: rift algo {bt.get('strategy', '')}",
        )

    # Grade B: Good but some concerns
    if (wf_deg >= 0.4 and mc_profit >= 75 and dd > -25 and ret > 0
            and mc_ruin <= 5):
        concern = ""
        if dd < -15:
            concern = " Drawdown is elevated — consider tighter stops."
        if mc_profit < 85:
            concern = " Monte Carlo shows some scenarios lose money."
        return (
            "B",
            f"Strategy shows promise with a real edge.{concern}",
            "Consider optimizing parameters before simulation.",
        )

    # Grade C: Marginal
    if (ret > 0 and mc_profit >= 50 and mc_ruin <= 20):
        return (
            "C",
            "Strategy is marginally profitable. Edge exists but is weak.",
            "Run parameter sweep: rift sweep <strategy> --pair <pair>",
        )

    # Grade D: Poor
    if ret > -10 and trades > 0:
        return (
            "D",
            "Strategy underperforms. Walk-forward or Monte Carlo shows significant weakness.",
            "Rethink strategy logic or try different parameters/pairs.",
        )

    # Grade F
    return (
        "F",
        "Strategy fails validation. Negative returns or critical weaknesses.",
        "Do not trade this strategy. Build a new one: rift research → Build",
    )
