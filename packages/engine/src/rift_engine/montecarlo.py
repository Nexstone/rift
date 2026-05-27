"""Monte Carlo simulation engine.

Resamples trade sequences to measure how much of a backtest result
was due to strategy edge vs lucky trade ordering.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from rift_engine.backtest import BacktestResult


@dataclass
class MonteCarloResult:
    """Results from Monte Carlo simulation."""

    strategy_name: str
    pair: str
    interval: str
    num_trades: int
    num_simulations: int
    original_return_pct: float

    # Percentile distribution of final returns
    p5: float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    p95: float

    # Drawdown distribution
    dd_p5: float
    dd_p25: float
    dd_p50: float
    dd_p75: float
    dd_p95: float

    # Risk metrics
    prob_profit: float  # % of simulations with positive return
    prob_ruin: float  # % of simulations with > 50% drawdown
    median_sharpe: float

    # Histogram buckets for ASCII chart
    histogram: list[dict]

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "pair": self.pair,
            "interval": self.interval,
            "num_trades": self.num_trades,
            "num_simulations": self.num_simulations,
            "original_return_pct": round(self.original_return_pct, 2),
            "return_distribution": {
                "p5": round(self.p5, 2),
                "p10": round(self.p10, 2),
                "p25": round(self.p25, 2),
                "p50": round(self.p50, 2),
                "p75": round(self.p75, 2),
                "p90": round(self.p90, 2),
                "p95": round(self.p95, 2),
            },
            "drawdown_distribution": {
                "p5": round(self.dd_p5, 2),
                "p25": round(self.dd_p25, 2),
                "p50": round(self.dd_p50, 2),
                "p75": round(self.dd_p75, 2),
                "p95": round(self.dd_p95, 2),
            },
            "prob_profit": round(self.prob_profit, 2),
            "prob_ruin": round(self.prob_ruin, 2),
            "median_sharpe": round(self.median_sharpe, 4),
            "histogram": self.histogram,
        }


def _build_histogram(returns: np.ndarray, num_buckets: int = 20) -> list[dict]:
    """Build histogram data for ASCII chart rendering. Clips to 5th-95th percentile range."""
    # Clip to 5th-95th percentile to avoid extreme outliers stretching the chart
    p5 = float(np.percentile(returns, 5))
    p95 = float(np.percentile(returns, 95))
    clipped = returns[(returns >= p5) & (returns <= p95)]
    if len(clipped) < 10:
        clipped = returns  # fall back if too few points after clipping
    min_val = float(np.min(clipped))
    max_val = float(np.max(clipped))

    if min_val == max_val:
        return [{"low": min_val, "high": max_val, "count": len(clipped), "pct": 100}]

    edges = np.linspace(min_val, max_val, num_buckets + 1)
    total = len(returns)
    buckets = []

    for i in range(num_buckets):
        low = float(edges[i])
        high = float(edges[i + 1])
        if i < num_buckets - 1:
            count = int(np.sum((returns >= low) & (returns < high)))
        else:
            count = int(np.sum((returns >= low) & (returns <= high)))

        buckets.append({
            "low": round(low, 1),
            "high": round(high, 1),
            "count": count,
            "pct": round(count / total * 100, 1),
        })

    return buckets


def run_montecarlo(
    backtest_result: BacktestResult,
    num_simulations: int = 10000,
    on_progress: callable = None,
) -> MonteCarloResult:
    """Run Monte Carlo simulation by resampling trade returns.

    Takes the per-trade PnL percentages from a backtest, reshuffles them
    thousands of times, and computes the distribution of possible outcomes.

    Args:
        backtest_result: A completed backtest result with trades
        num_simulations: Number of random paths to generate (default 10,000)
        on_progress: Optional callback(pct, msg)
    """
    trades = backtest_result.trades
    if not trades:
        raise ValueError("Cannot run Monte Carlo — backtest has no trades. Run a backtest that generates trades first.")

    # Extract per-trade return percentages
    trade_returns = np.array([t.pnl_pct / 100 for t in trades])  # convert to decimal
    num_trades = len(trade_returns)

    if on_progress:
        on_progress(10, f"Simulating {num_simulations} paths with {num_trades} trades each...")

    # Generate all random paths at once (vectorized)
    # Each row is one simulation, each column is one trade position
    indices = np.random.randint(0, num_trades, size=(num_simulations, num_trades))
    sampled_returns = trade_returns[indices]  # (num_simulations, num_trades)

    if on_progress:
        on_progress(40, "Computing equity curves...")

    # Compute cumulative equity for each simulation
    equity_paths = np.cumprod(1 + sampled_returns, axis=1)  # (num_simulations, num_trades)
    final_returns = (equity_paths[:, -1] - 1) * 100  # final return in percentage

    if on_progress:
        on_progress(60, "Computing drawdowns...")

    # Compute max drawdown for each simulation
    running_max = np.maximum.accumulate(equity_paths, axis=1)
    drawdowns = (equity_paths - running_max) / running_max * 100  # in percentage
    max_drawdowns = np.min(drawdowns, axis=1)  # most negative drawdown per path

    if on_progress:
        on_progress(80, "Computing statistics...")

    # Compute per-path Sharpe (simplified: mean return / std return)
    path_returns = np.diff(equity_paths, axis=1) / equity_paths[:, :-1]
    path_means = np.mean(path_returns, axis=1)
    path_stds = np.std(path_returns, axis=1)
    path_stds[path_stds == 0] = 1e-10  # avoid division by zero
    # Annualize based on interval from the backtest result
    from rift_engine.backtest import _periods_per_year
    periods = _periods_per_year(backtest_result.interval)
    path_sharpes = path_means / path_stds * np.sqrt(periods)

    # Build histogram
    histogram = _build_histogram(final_returns)

    if on_progress:
        on_progress(100, "Monte Carlo simulation complete")

    return MonteCarloResult(
        strategy_name=backtest_result.strategy_name,
        pair=backtest_result.pair,
        interval=backtest_result.interval,
        num_trades=num_trades,
        num_simulations=num_simulations,
        original_return_pct=backtest_result.total_return_pct,
        # Return distribution
        p5=float(np.percentile(final_returns, 5)),
        p10=float(np.percentile(final_returns, 10)),
        p25=float(np.percentile(final_returns, 25)),
        p50=float(np.percentile(final_returns, 50)),
        p75=float(np.percentile(final_returns, 75)),
        p90=float(np.percentile(final_returns, 90)),
        p95=float(np.percentile(final_returns, 95)),
        # Drawdown distribution
        dd_p5=float(np.percentile(max_drawdowns, 5)),
        dd_p25=float(np.percentile(max_drawdowns, 25)),
        dd_p50=float(np.percentile(max_drawdowns, 50)),
        dd_p75=float(np.percentile(max_drawdowns, 75)),
        dd_p95=float(np.percentile(max_drawdowns, 95)),
        # Risk
        prob_profit=float(np.mean(final_returns > 0) * 100),
        prob_ruin=float(np.mean(max_drawdowns < -50) * 100),
        median_sharpe=float(np.median(path_sharpes)),
        histogram=histogram,
    )
