"""Walk-forward analysis engine.

Splits data into rolling train/test windows, runs backtests on each,
and aggregates out-of-sample results to measure strategy robustness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import polars as pl

from rift_engine.backtest import BacktestResult, run_backtest
from rift_engine.strategy import Strategy


@dataclass
class WindowResult:
    """Results from a single walk-forward window."""

    window_num: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    in_sample: BacktestResult
    out_of_sample: BacktestResult

    def to_dict(self) -> dict:
        return {
            "window": self.window_num,
            "train_period": {
                "start": datetime.fromtimestamp(self.train_start / 1000).strftime("%Y-%m-%d"),
                "end": datetime.fromtimestamp(self.train_end / 1000).strftime("%Y-%m-%d"),
                "candles": self.in_sample.num_trades,  # will be overridden below
            },
            "test_period": {
                "start": datetime.fromtimestamp(self.test_start / 1000).strftime("%Y-%m-%d"),
                "end": datetime.fromtimestamp(self.test_end / 1000).strftime("%Y-%m-%d"),
            },
            "in_sample": {
                "return_pct": round(self.in_sample.total_return_pct, 2),
                "sharpe": round(self.in_sample.sharpe_ratio, 4),
                "trades": self.in_sample.num_trades,
                "win_rate": round(self.in_sample.win_rate, 2),
                "max_drawdown_pct": round(self.in_sample.max_drawdown_pct, 2),
            },
            "out_of_sample": {
                "return_pct": round(self.out_of_sample.total_return_pct, 2),
                "sharpe": round(self.out_of_sample.sharpe_ratio, 4),
                "trades": self.out_of_sample.num_trades,
                "win_rate": round(self.out_of_sample.win_rate, 2),
                "max_drawdown_pct": round(self.out_of_sample.max_drawdown_pct, 2),
            },
        }


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward analysis results."""

    strategy_name: str
    pair: str
    interval: str
    train_months: int
    test_months: int
    num_windows: int
    windows: list[WindowResult] = field(default_factory=list)

    # Aggregated in-sample metrics
    avg_is_return: float = 0.0
    avg_is_sharpe: float = 0.0
    avg_is_win_rate: float = 0.0
    avg_is_max_dd: float = 0.0
    total_is_trades: int = 0

    # Aggregated out-of-sample metrics
    avg_oos_return: float = 0.0
    avg_oos_sharpe: float = 0.0
    avg_oos_win_rate: float = 0.0
    avg_oos_max_dd: float = 0.0
    total_oos_trades: int = 0

    # Combined out-of-sample equity curve
    combined_oos_return: float = 0.0

    # Key metric: degradation ratio
    degradation_ratio: float = 0.0  # OOS Sharpe / IS Sharpe

    # Robustness score
    pct_profitable_windows: float = 0.0  # % of OOS windows with positive return

    # Monte Carlo permutation test
    mc_p_value: float = 1.0              # probability that random shuffle beats real strategy
    mc_real_sharpe: float = 0.0
    mc_median_random_sharpe: float = 0.0
    mc_significant: bool = False         # True if p < 0.05

    # Regime analysis
    regime_results: dict = field(default_factory=dict)  # {regime: {sharpe, return_pct, trades, win_rate}}

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "pair": self.pair,
            "interval": self.interval,
            "config": f"{self.train_months}m train / {self.test_months}m test",
            "num_windows": self.num_windows,
            "in_sample": {
                "avg_return_pct": round(self.avg_is_return, 2),
                "avg_sharpe": round(self.avg_is_sharpe, 4),
                "avg_win_rate": round(self.avg_is_win_rate, 2),
                "avg_max_drawdown_pct": round(self.avg_is_max_dd, 2),
                "total_trades": self.total_is_trades,
            },
            "out_of_sample": {
                "avg_return_pct": round(self.avg_oos_return, 2),
                "avg_sharpe": round(self.avg_oos_sharpe, 4),
                "avg_win_rate": round(self.avg_oos_win_rate, 2),
                "avg_max_drawdown_pct": round(self.avg_oos_max_dd, 2),
                "total_trades": self.total_oos_trades,
                "combined_return_pct": round(self.combined_oos_return, 2),
            },
            "degradation_ratio": round(self.degradation_ratio, 4),
            "pct_profitable_windows": round(self.pct_profitable_windows, 2),
            "monte_carlo": {
                "p_value": round(self.mc_p_value, 4),
                "real_sharpe": round(self.mc_real_sharpe, 4),
                "median_random_sharpe": round(self.mc_median_random_sharpe, 4),
                "significant": bool(self.mc_significant),
            },
            "regime_analysis": self.regime_results,
            "windows": [w.to_dict() for w in self.windows],
        }


def parse_walk_forward_config(config: str) -> tuple[int, int]:
    """Parse walk-forward config string like '3m/1m' into (train_months, test_months)."""
    match = re.match(r"(\d+)m\s*/\s*(\d+)m", config)
    if not match:
        raise ValueError(f"Invalid walk-forward config '{config}'. Expected format: '3m/1m' (train/test)")
    return int(match.group(1)), int(match.group(2))


def _split_windows(
    df: pl.DataFrame,
    train_months: int,
    test_months: int,
) -> list[tuple[pl.DataFrame, pl.DataFrame]]:
    """Split candle data into rolling train/test windows."""
    timestamps = df["timestamp"].to_numpy()
    min_ts = int(timestamps[0])
    max_ts = int(timestamps[-1])

    train_ms = train_months * 30 * 24 * 60 * 60 * 1000  # approximate months in ms
    test_ms = test_months * 30 * 24 * 60 * 60 * 1000
    step_ms = test_ms  # step forward by test_months each window

    windows = []
    cursor = min_ts

    while cursor + train_ms + test_ms <= max_ts:
        train_start = cursor
        train_end = cursor + train_ms
        test_start = train_end
        test_end = test_start + test_ms

        train_df = df.filter(
            (pl.col("timestamp") >= train_start) & (pl.col("timestamp") < train_end)
        )
        test_df = df.filter(
            (pl.col("timestamp") >= test_start) & (pl.col("timestamp") < test_end)
        )

        if len(train_df) > 0 and len(test_df) > 0:
            windows.append((train_df, test_df))

        cursor += step_ms

    return windows


def run_walk_forward(
    strategy: Strategy,
    df: pl.DataFrame,
    strategy_name: str,
    pair: str,
    interval: str,
    train_months: int,
    test_months: int,
    initial_equity: float = 10000.0,
    fee_rate: float = 0.00034,  # Blended: 70% maker (-0.01%) + 30% taker (0.035%) + 0.03% builder
    leverage: float = 1.0,
    on_progress: callable = None,
    funding_df: pl.DataFrame | None = None,
    strategy_cls: type | None = None,
    oi_df: pl.DataFrame | None = None,
) -> WalkForwardResult:
    """Run walk-forward analysis.

    Args:
        strategy: Strategy instance to test
        df: Full candle DataFrame
        strategy_name: Name for reporting
        pair: Trading pair
        interval: Candle interval
        train_months: Number of months for training window
        test_months: Number of months for testing window
        initial_equity: Starting equity per window
        fee_rate: Trading fee per side
        leverage: Position leverage
        on_progress: Optional callback(pct, msg) for progress updates
    """
    windows = _split_windows(df, train_months, test_months)

    if not windows:
        raise ValueError(
            f"Not enough data for walk-forward with {train_months}m/{test_months}m windows. "
            f"Need at least {train_months + test_months} months of data."
        )

    result = WalkForwardResult(
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        train_months=train_months,
        test_months=test_months,
        num_windows=len(windows),
    )

    is_returns = []
    is_sharpes = []
    is_win_rates = []
    is_max_dds = []
    oos_returns = []
    oos_sharpes = []
    oos_win_rates = []
    oos_max_dds = []
    oos_equity_multipliers = []

    for i, (train_df, test_df) in enumerate(windows):
        if on_progress:
            pct = int((i / len(windows)) * 100)
            train_start_str = datetime.fromtimestamp(int(train_df["timestamp"].min()) / 1000).strftime("%Y-%m-%d")
            test_end_str = datetime.fromtimestamp(int(test_df["timestamp"].max()) / 1000).strftime("%Y-%m-%d")
            on_progress(pct, f"Window {i+1}/{len(windows)}: {train_start_str} → {test_end_str}")

        # Create fresh strategy instances per window to prevent state leakage
        if strategy_cls is not None:
            is_strategy = strategy_cls()
            oos_strategy = strategy_cls()
        else:
            # Fallback: try to create from the class of the passed instance
            is_strategy = strategy.__class__()
            oos_strategy = strategy.__class__()

        # Filter funding data to match window if available
        train_funding = None
        test_funding = None
        if funding_df is not None and len(funding_df) > 0:
            train_start_ts = int(train_df["timestamp"].min())
            train_end_ts = int(train_df["timestamp"].max())
            test_start_ts = int(test_df["timestamp"].min())
            test_end_ts = int(test_df["timestamp"].max())
            train_funding = funding_df.filter(
                (pl.col("timestamp") >= train_start_ts) & (pl.col("timestamp") <= train_end_ts)
            )
            test_funding = funding_df.filter(
                (pl.col("timestamp") >= test_start_ts) & (pl.col("timestamp") <= test_end_ts)
            )

        # Filter OI data to match window
        train_oi = None
        test_oi = None
        if oi_df is not None and len(oi_df) > 0:
            train_oi = oi_df.filter(
                (pl.col("timestamp") >= train_start_ts) & (pl.col("timestamp") <= train_end_ts)
            )
            test_oi = oi_df.filter(
                (pl.col("timestamp") >= test_start_ts) & (pl.col("timestamp") <= test_end_ts)
            )

        # Run in-sample backtest (train period) — fresh strategy instance
        is_result = run_backtest(
            strategy=is_strategy,
            df=train_df,
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            initial_equity=initial_equity,
            fee_rate=fee_rate,
            leverage=leverage,
            silent=True,
            funding_df=train_funding,
            oi_df=train_oi,
            use_fractional_sizing=True,
        )

        # Prepare OOS strategy with training data (no lookahead)
        # Strategies with prepare() use train_df for model training, test_df for prediction
        if hasattr(oos_strategy, 'prepare'):
            oos_strategy.prepare(test_df, train_df=train_df, funding_df=test_funding, pair=pair)

        # Run out-of-sample backtest — prepare() handled externally, skip in engine
        oos_result = run_backtest(
            strategy=oos_strategy,
            df=test_df,
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            initial_equity=initial_equity,
            fee_rate=fee_rate,
            leverage=leverage,
            silent=True,
            funding_df=test_funding,
            oi_df=test_oi,
            use_fractional_sizing=True,
            skip_prepare=True,
        )

        window_result = WindowResult(
            window_num=i + 1,
            train_start=int(train_df["timestamp"].min()),
            train_end=int(train_df["timestamp"].max()),
            test_start=int(test_df["timestamp"].min()),
            test_end=int(test_df["timestamp"].max()),
            in_sample=is_result,
            out_of_sample=oos_result,
        )
        result.windows.append(window_result)

        is_returns.append(is_result.total_return_pct)
        is_sharpes.append(is_result.sharpe_ratio)
        is_win_rates.append(is_result.win_rate)
        is_max_dds.append(is_result.max_drawdown_pct)
        result.total_is_trades += is_result.num_trades

        oos_returns.append(oos_result.total_return_pct)
        oos_sharpes.append(oos_result.sharpe_ratio)
        oos_win_rates.append(oos_result.win_rate)
        oos_max_dds.append(oos_result.max_drawdown_pct)
        result.total_oos_trades += oos_result.num_trades

        # Track cumulative OOS equity
        oos_multiplier = 1 + (oos_result.total_return_pct / 100)
        oos_equity_multipliers.append(oos_multiplier)

    # Aggregate
    result.avg_is_return = float(np.mean(is_returns))
    result.avg_is_sharpe = float(np.mean(is_sharpes))
    result.avg_is_win_rate = float(np.mean(is_win_rates))
    result.avg_is_max_dd = float(np.mean(is_max_dds))

    result.avg_oos_return = float(np.mean(oos_returns))
    result.avg_oos_sharpe = float(np.mean(oos_sharpes))
    result.avg_oos_win_rate = float(np.mean(oos_win_rates))
    result.avg_oos_max_dd = float(np.mean(oos_max_dds))

    # Combined OOS return (compounding across windows)
    combined_multiplier = 1.0
    for m in oos_equity_multipliers:
        combined_multiplier *= m
    result.combined_oos_return = (combined_multiplier - 1) * 100

    # Degradation ratio: OOS Sharpe / IS Sharpe
    if result.avg_is_sharpe != 0:
        result.degradation_ratio = result.avg_oos_sharpe / result.avg_is_sharpe
    else:
        result.degradation_ratio = 0.0

    # Percentage of profitable OOS windows
    profitable = sum(1 for r in oos_returns if r > 0)
    result.pct_profitable_windows = (profitable / len(oos_returns)) * 100 if oos_returns else 0

    # ─── POST-VALIDATION: Monte Carlo + Regime Split ───
    all_oos_trades = []
    for w in result.windows:
        all_oos_trades.extend(w.out_of_sample.trades)

    if len(all_oos_trades) >= 10:
        if on_progress:
            on_progress(95, "Running Monte Carlo permutation test...")
        mc = monte_carlo_permutation(all_oos_trades)
        result.mc_p_value = mc["p_value"]
        result.mc_real_sharpe = mc["real_sharpe"]
        result.mc_median_random_sharpe = mc["median_random_sharpe"]
        result.mc_significant = mc["significant"]

        if on_progress:
            on_progress(98, "Running regime analysis...")
        result.regime_results = regime_split(all_oos_trades, df)

    if on_progress:
        on_progress(100, "Walk-forward analysis complete")

    return result


# ──────────────────────────────────────────────────────────────
#  Monte Carlo Permutation Test
# ──────────────────────────────────────────────────────────────
def monte_carlo_permutation(
    trades: list,
    n_permutations: int = 10_000,
) -> dict:
    """Randomly flip trade signs to test if the strategy's edge is real.

    For each permutation, randomly multiply each trade return by +1 or -1
    (simulating random long/short assignment). If the real strategy's total
    return beats random assignment less than 95% of the time, the edge
    may not be statistically significant.

    This tests whether the strategy's DIRECTIONAL DECISIONS add value
    over random direction picking.

    Returns:
        {"p_value", "real_sharpe", "median_random_sharpe", "significant"}
    """
    returns = np.array([t.pnl_pct for t in trades])
    n = len(returns)

    if n < 5:
        return {"p_value": 1.0, "real_sharpe": 0.0, "median_random_sharpe": 0.0, "significant": False}

    # Real strategy metrics
    real_total = float(np.sum(returns))
    real_std = float(np.std(returns, ddof=1))
    real_sharpe = (np.mean(returns) / real_std * np.sqrt(252)) if real_std > 0 else 0.0

    # Permutation test: randomly flip sign of each return
    rng = np.random.default_rng(42)
    abs_returns = np.abs(returns)
    random_totals = np.zeros(n_permutations)
    random_sharpes = np.zeros(n_permutations)

    for i in range(n_permutations):
        signs = rng.choice([-1.0, 1.0], size=n)
        randomized = abs_returns * signs
        random_totals[i] = np.sum(randomized)
        r_std = np.std(randomized, ddof=1)
        random_sharpes[i] = (np.mean(randomized) / r_std * np.sqrt(252)) if r_std > 0 else 0.0

    # p-value: fraction of random trials that beat real total return
    beats_real = np.sum(random_totals >= real_total)
    p_value = beats_real / n_permutations

    return {
        "p_value": round(float(p_value), 4),
        "real_sharpe": round(float(real_sharpe), 4),
        "median_random_sharpe": round(float(np.median(random_sharpes)), 4),
        "significant": p_value < 0.05,
    }


# ──────────────────────────────────────────────────────────────
#  Regime Split Analysis
# ──────────────────────────────────────────────────────────────
def regime_split(
    trades: list,
    price_df: pl.DataFrame,
) -> dict:
    """Split trades by market regime and report per-regime performance.

    Regime is determined by BTC's 30-day return at the time of each trade:
        Bull:     > +5%
        Bear:     < -5%
        Sideways: -5% to +5%

    Returns:
        {"bull": {sharpe, return_pct, trades, win_rate}, "bear": {...}, "sideways": {...}}
    """
    # Build a regime lookup from price data
    closes = price_df["close"].to_list()
    timestamps = price_df["timestamp"].to_list() if "timestamp" in price_df.columns else []

    # Compute 30-period rolling return for regime classification
    regime_at_ts: dict[int, str] = {}
    lookback = 720  # 30 days * 24 hours for 1h candles

    for i in range(lookback, len(closes)):
        ret_30d = (closes[i] - closes[i - lookback]) / closes[i - lookback] if closes[i - lookback] > 0 else 0

        if ret_30d > 0.05:
            regime = "bull"
        elif ret_30d < -0.05:
            regime = "bear"
        else:
            regime = "sideways"

        if i < len(timestamps):
            regime_at_ts[timestamps[i]] = regime

    # Classify each trade
    regime_trades: dict[str, list] = {"bull": [], "bear": [], "sideways": []}

    for t in trades:
        # Find closest timestamp
        best_regime = "sideways"  # default
        if regime_at_ts:
            # Find regime at trade entry time
            best_diff = float("inf")
            for ts, reg in regime_at_ts.items():
                diff = abs(ts - t.entry_time)
                if diff < best_diff:
                    best_diff = diff
                    best_regime = reg
        regime_trades[best_regime].append(t)

    # Compute per-regime metrics
    results = {}
    for regime, rtrades in regime_trades.items():
        if not rtrades:
            results[regime] = {
                "sharpe": 0.0, "return_pct": 0.0, "trades": 0,
                "win_rate": 0.0, "allocation_score": 0.0,
            }
            continue

        returns = [t.pnl_pct for t in rtrades]
        wins = sum(1 for r in returns if r > 0)
        total_return = sum(returns)
        mean_r = np.mean(returns)
        std_r = np.std(returns, ddof=1) if len(returns) > 1 else 1.0
        sharpe = (mean_r / std_r * np.sqrt(252)) if std_r > 0 else 0.0

        # Allocation score: how much capital to allocate in this regime
        # Based on Sharpe and win rate — needs both positive edge AND consistency
        # Score 0.0 = sit out, 1.0 = full allocation
        if sharpe <= 0 or wins / len(rtrades) < 0.25:
            alloc_score = 0.0  # negative edge or terrible WR → don't trade
        elif len(rtrades) < 5:
            alloc_score = 0.2  # insufficient data → minimal allocation
        else:
            # Scale linearly: Sharpe 0→0.2, Sharpe 1→0.7, Sharpe 2+→1.0
            alloc_score = min(1.0, 0.2 + float(sharpe) * 0.4)

        results[regime] = {
            "sharpe": round(float(sharpe), 4),
            "return_pct": round(float(total_return), 2),
            "trades": len(rtrades),
            "win_rate": round(wins / len(rtrades) * 100, 1),
            "allocation_score": round(float(alloc_score), 2),
        }

    return results
