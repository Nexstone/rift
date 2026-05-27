"""Smart parameter optimization using Optuna (Bayesian optimization).

Replaces brute-force grid sweep with intelligent search that finds optimal
parameters in ~50-100 trials instead of hundreds/thousands. Uses Tree-structured
Parzen Estimator (TPE) to learn which parameter regions produce good results
and focus search there.

Usage:
    from rift_engine.smart_optimize import smart_sweep
    result = smart_sweep(strategy_cls, df, funding_df, param_ranges, n_trials=80)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

# Suppress Optuna's verbose logging
logging.getLogger("optuna").setLevel(logging.WARNING)


@dataclass
class OptimizeResult:
    """Result of smart parameter optimization."""
    best_params: dict[str, Any]
    best_return: float
    best_sharpe: float
    best_win_rate: float
    best_max_dd: float
    n_trials: int
    n_completed: int
    improvement_vs_default: float  # % improvement over default params


def smart_sweep(
    strategy_cls: type,
    df: 'pl.DataFrame',
    param_ranges: dict[str, tuple],
    funding_df: 'pl.DataFrame | None' = None,
    oi_df: 'pl.DataFrame | None' = None,
    pair: str = "BTC",
    interval: str = "1h",
    initial_equity: float = 10000.0,
    n_trials: int = 80,
    optimize_target: str = "sharpe",  # "sharpe", "return", "calmar"
    on_progress: callable = None,
) -> OptimizeResult:
    """Run Bayesian parameter optimization using Optuna.

    Args:
        strategy_cls: Strategy class to optimize
        df: Candle DataFrame
        param_ranges: Dict of {param_name: (min, max, step)} for floats/ints
                      or {param_name: [value1, value2, ...]} for categoricals
        funding_df: Optional funding DataFrame
        oi_df: Optional OI DataFrame
        pair: Trading pair
        interval: Candle interval
        initial_equity: Starting equity
        n_trials: Number of optimization trials (50-100 is usually sufficient)
        optimize_target: What to maximize ("sharpe", "return", "calmar")
        on_progress: Optional callback(trial_num, n_trials, best_so_far)
    """
    import optuna
    import polars as pl
    from rift_engine.backtest import run_backtest

    # Run default backtest for comparison
    default_strategy = strategy_cls()
    default_result = run_backtest(
        strategy=default_strategy, df=df, strategy_name="default",
        pair=pair, interval=interval, initial_equity=initial_equity,
        funding_df=funding_df, oi_df=oi_df, silent=True,
    )

    def objective(trial):
        # Build config from trial suggestions
        config_kwargs = {}
        for param_name, param_range in param_ranges.items():
            if isinstance(param_range, list):
                # Categorical
                config_kwargs[param_name] = trial.suggest_categorical(param_name, param_range)
            elif isinstance(param_range, tuple) and len(param_range) == 3:
                low, high, step = param_range
                if isinstance(low, int) and isinstance(high, int):
                    config_kwargs[param_name] = trial.suggest_int(param_name, low, high, step=step)
                else:
                    config_kwargs[param_name] = trial.suggest_float(param_name, float(low), float(high), step=float(step))
            elif isinstance(param_range, tuple) and len(param_range) == 2:
                low, high = param_range
                if isinstance(low, int) and isinstance(high, int):
                    config_kwargs[param_name] = trial.suggest_int(param_name, low, high)
                else:
                    config_kwargs[param_name] = trial.suggest_float(param_name, float(low), float(high))

        # Create strategy with suggested params
        try:
            import dataclasses
            if strategy_cls.config_class:
                defaults = {}
                for f in dataclasses.fields(strategy_cls.config_class):
                    if f.default is not dataclasses.MISSING:
                        defaults[f.name] = f.default
                defaults.update(config_kwargs)
                config = strategy_cls.config_class(**defaults)
                strategy = strategy_cls(config=config)
            else:
                strategy = strategy_cls()
        except Exception:
            return float('-inf')

        # Run backtest
        try:
            result = run_backtest(
                strategy=strategy, df=df, strategy_name="optuna",
                pair=pair, interval=interval, initial_equity=initial_equity,
                funding_df=funding_df, oi_df=oi_df, silent=True,
            )
        except Exception:
            return float('-inf')

        # Require minimum trades
        if result.num_trades < 5:
            return float('-inf')

        # Progress callback
        if on_progress:
            try:
                best_so_far = trial.study.best_value
            except ValueError:
                best_so_far = 0.0
            on_progress(trial.number + 1, n_trials, best_so_far)

        # Return optimization target
        if optimize_target == "sharpe":
            return result.sharpe_ratio
        elif optimize_target == "return":
            return result.total_return_pct
        elif optimize_target == "calmar":
            return result.calmar_ratio
        else:
            return result.sharpe_ratio

    # Create and run study
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    # Get best result
    best = study.best_trial
    best_params = best.params

    # Run final backtest with best params to get full metrics
    import dataclasses
    if strategy_cls.config_class:
        defaults = {}
        for f in dataclasses.fields(strategy_cls.config_class):
            if f.default is not dataclasses.MISSING:
                defaults[f.name] = f.default
        defaults.update(best_params)
        config = strategy_cls.config_class(**defaults)
        best_strategy = strategy_cls(config=config)
    else:
        best_strategy = strategy_cls()

    best_result = run_backtest(
        strategy=best_strategy, df=df, strategy_name="optimized",
        pair=pair, interval=interval, initial_equity=initial_equity,
        funding_df=funding_df, oi_df=oi_df, silent=True,
    )

    # Compute improvement
    default_val = default_result.sharpe_ratio if optimize_target == "sharpe" else default_result.total_return_pct
    best_val = best_result.sharpe_ratio if optimize_target == "sharpe" else best_result.total_return_pct
    improvement = ((best_val - default_val) / abs(default_val) * 100) if default_val != 0 else 0

    return OptimizeResult(
        best_params=best_params,
        best_return=best_result.total_return_pct,
        best_sharpe=best_result.sharpe_ratio,
        best_win_rate=best_result.win_rate,
        best_max_dd=best_result.max_drawdown_pct,
        n_trials=n_trials,
        n_completed=len(study.trials),
        improvement_vs_default=round(improvement, 1),
    )


def feature_importance(
    strategy_cls: type,
    df: 'pl.DataFrame',
    funding_df: 'pl.DataFrame | None' = None,
    oi_df: 'pl.DataFrame | None' = None,
    pair: str = "BTC",
    interval: str = "1h",
) -> dict[str, float]:
    """Use XGBoost to rank which features predict profitable trades.

    Trains a classifier on all indicator values at each trade entry point,
    labels by whether the trade was profitable. Returns feature importance
    scores — higher = more predictive.
    """
    import xgboost as xgb
    from rift_engine.backtest import run_backtest

    # Run backtest to get trades with indicator context
    strategy = strategy_cls()
    result = run_backtest(
        strategy=strategy, df=df, strategy_name="fi",
        pair=pair, interval=interval,
        funding_df=funding_df, oi_df=oi_df, silent=True,
    )

    if result.num_trades < 20:
        return {}

    # Build feature matrix from candle data at trade entry points
    import polars as pl
    closes = df["close"].to_numpy().astype(float)
    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    volumes = df["volume"].to_numpy().astype(float)
    timestamps = df["timestamp"].to_numpy()

    from rift_engine.backtest import _compute_indicator
    from rift_engine.strategy import Indicator

    # Compute a broad set of indicators
    indicator_defs = {
        "rsi": Indicator("rsi", period=14),
        "ema_20": Indicator("ema", period=20),
        "ema_50": Indicator("ema", period=50),
        "atr": Indicator("atr", period=14),
        "adx": Indicator("adx", period=14),
        "bbwidth": Indicator("bbands_width", period=20, std=2.0),
        "stoch_k": Indicator("stoch_k", period=14, smooth=3),
        "cci": Indicator("cci", period=20),
        "roc": Indicator("roc", period=12),
        "supertrend": Indicator("supertrend", period=10, mult=3.0),
        "vol_ratio": Indicator("vol_ratio", period=20),
        "cmf": Indicator("cmf", period=20),
        "williams_r": Indicator("williams_r", period=14),
        "linreg": Indicator("linreg_slope", period=20),
        "aroon_up": Indicator("aroon_up", period=25),
        "aroon_down": Indicator("aroon_down", period=25),
    }

    computed = {}
    for name, ind in indicator_defs.items():
        series = _compute_indicator(name, ind, closes, highs, lows, volumes)
        computed[name] = series

    # Build training data: features at each trade entry, label = win/loss
    X = []
    y = []
    for trade in result.trades:
        # Find candle index closest to trade entry
        idx = np.searchsorted(timestamps, trade.entry_time)
        if idx >= len(closes) or idx < 50:
            continue

        features = []
        for name in indicator_defs:
            val = computed[name][idx]
            features.append(val if np.isfinite(val) else 0.0)

        # Add price-derived features
        features.append(closes[idx] / closes[idx - 1] - 1 if closes[idx - 1] > 0 else 0)  # 1-bar return
        features.append(closes[idx] / closes[idx - 5] - 1 if closes[idx - 5] > 0 else 0)  # 5-bar return
        features.append(volumes[idx] / np.mean(volumes[max(0, idx - 20):idx]) if np.mean(volumes[max(0, idx - 20):idx]) > 0 else 1)  # relative volume

        X.append(features)
        y.append(1 if trade.pnl > 0 else 0)

    if len(X) < 20:
        return {}

    X = np.array(X)
    y = np.array(y)

    # Train XGBoost
    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        use_label_encoder=False,
        eval_metric='logloss',
        verbosity=0,
    )
    model.fit(X, y)

    # Get feature importance
    feature_names = list(indicator_defs.keys()) + ["return_1bar", "return_5bar", "relative_vol"]
    importances = model.feature_importances_

    result_dict = {}
    for name, imp in sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True):
        result_dict[name] = round(float(imp), 4)

    return result_dict
