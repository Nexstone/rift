"""Portfolio backtesting system.

Currently implements:
- Multi-strategy backtesting with capital allocation
- Per-strategy P&L tracking (independent backtests with allocated equity)
- Strategy return correlation analysis
- Combined portfolio equity curve and metrics

Planned for live trading (not yet implemented):
- Virtual position ledger for netted positions
- Position netting for single-account exchanges
- Real-time risk management (drawdown limits, kill switch)
- Funding rate attribution across netted positions
- Risk-parity and dynamic allocation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from rift_engine.backtest import run_backtest, BacktestResult
from rift_engine.strategy import Strategy, discover_strategies, get_strategy


# VirtualPosition and position netting are planned for live portfolio trading.
# Currently, portfolio backtesting runs strategies independently.


@dataclass
class StrategyAllocation:
    """Configuration for one strategy in the portfolio."""
    name: str
    pair: str
    timeframe: str
    allocation: float  # fraction of total capital (0.0 - 1.0)
    strategy_instance: Strategy | None = None


@dataclass
class StrategyResult:
    """Per-strategy results within a portfolio backtest."""
    name: str
    pair: str
    allocation: float
    equity_start: float
    equity_end: float
    return_pct: float
    num_trades: int
    win_rate: float
    sharpe: float
    profit_factor: float
    max_drawdown_pct: float
    funding_pnl: float
    trades: list = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    monthly_returns: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pair": self.pair,
            "allocation": f"{self.allocation * 100:.0f}%",
            "equity_start": round(self.equity_start, 2),
            "equity_end": round(self.equity_end, 2),
            "return_pct": round(self.return_pct, 2),
            "num_trades": self.num_trades,
            "win_rate": round(self.win_rate, 2),
            "sharpe": round(self.sharpe, 4),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "funding_pnl": round(self.funding_pnl, 2),
            "monthly_returns": {k: round(v, 2) for k, v in self.monthly_returns.items()},
        }


@dataclass
class PortfolioResult:
    """Combined portfolio backtest results."""
    initial_equity: float
    final_equity: float
    total_return_pct: float
    portfolio_sharpe: float
    portfolio_max_drawdown_pct: float
    total_trades: int
    strategy_results: list[StrategyResult] = field(default_factory=list)
    portfolio_equity_curve: list[float] = field(default_factory=list)
    correlation_matrix: dict | None = None

    def to_dict(self) -> dict:
        return {
            "initial_equity": self.initial_equity,
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "portfolio_sharpe": round(self.portfolio_sharpe, 4),
            "portfolio_max_drawdown_pct": round(self.portfolio_max_drawdown_pct, 2),
            "total_trades": self.total_trades,
            "strategies": [s.to_dict() for s in self.strategy_results],
            "correlation_matrix": self.correlation_matrix,
        }


def load_portfolio_config(config_path: str) -> dict:
    """Load portfolio config from YAML file."""
    import yaml
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Portfolio config not found: {config_path}")

    return yaml.safe_load(path.read_text())


def run_portfolio_backtest(
    config: dict,
    strategies_dir: str = "",
    on_progress: callable = None,
) -> PortfolioResult:
    """Run a portfolio backtest with multiple strategies.

    Each strategy runs independently on its allocated capital.
    Results are combined into a portfolio-level view with
    correlation analysis and combined equity curve.

    Args:
        config: Portfolio config dict with strategies, allocations, risk limits
        strategies_dir: Directory with strategy .py files
        on_progress: Optional progress callback(pct, msg)
    """
    from pathlib import Path
    from rift_data.historical import load_candles_smart, load_funding_smart

    initial_equity = config.get("initial_equity", 10000)
    strategy_configs = config.get("strategies", [])
    risk_config = config.get("risk", {})

    if not strategy_configs:
        raise ValueError("No strategies defined in portfolio config")

    # Validate allocations sum to <= 1.0
    total_alloc = sum(s.get("allocation", 0) for s in strategy_configs)
    if total_alloc > 1.01:
        raise ValueError(f"Strategy allocations sum to {total_alloc:.2f} — must be <= 1.0")

    # Discover strategies
    dirs = [Path(__file__).parent.parent.parent.parent.parent / "strategies"]
    if strategies_dir:
        dirs.append(Path(strategies_dir))
    discover_strategies(dirs)

    # Run each strategy independently
    strategy_results: list[StrategyResult] = []
    strategy_equity_curves: list[list[float]] = []
    total_strategies = len(strategy_configs)

    for idx, strat_config in enumerate(strategy_configs):
        name = strat_config["name"]
        pair = strat_config.get("pair", "BTC-PERP")
        timeframe = strat_config.get("timeframe", "1h")
        allocation = strat_config.get("allocation", 1.0 / total_strategies)
        allocated_equity = initial_equity * allocation

        if on_progress:
            pct = int((idx / total_strategies) * 100)
            on_progress(pct, f"Backtesting {name} on {pair} ({allocation * 100:.0f}% allocation)...")

        # Load strategy
        try:
            strategy_cls = get_strategy(name)
        except KeyError as e:
            raise ValueError(f"Strategy '{name}' not found: {e}")

        strategy = strategy_cls()

        # Load data
        from rift_data.data import normalize_coin
        coin = normalize_coin(pair)
        df = load_candles_smart(coin, timeframe)
        if df is None or len(df) == 0:
            raise ValueError(f"No data for {pair} {timeframe}. Check bundled data or run 'rift data fetch --pair {pair} --tf {timeframe}'.")

        funding_df = load_funding_smart(coin)

        # Run backtest with allocated equity
        bt = run_backtest(
            strategy=strategy,
            df=df,
            strategy_name=name,
            pair=pair,
            interval=timeframe,
            initial_equity=allocated_equity,
            funding_df=funding_df,
            silent=True,
            use_fractional_sizing=True,
        )

        # Build strategy result
        sr = StrategyResult(
            name=name,
            pair=pair,
            allocation=allocation,
            equity_start=allocated_equity,
            equity_end=bt.final_equity,
            return_pct=bt.total_return_pct,
            num_trades=bt.num_trades,
            win_rate=bt.win_rate,
            sharpe=bt.sharpe_ratio,
            profit_factor=bt.profit_factor,
            max_drawdown_pct=bt.max_drawdown_pct,
            funding_pnl=bt.total_funding,
            equity_curve=bt.equity_curve,
            monthly_returns=bt.monthly_returns,
        )
        strategy_results.append(sr)
        strategy_equity_curves.append(bt.equity_curve)

    if on_progress:
        on_progress(90, "Computing portfolio metrics...")

    # Build combined portfolio equity curve
    # Normalize all curves to the same length (shortest)
    min_len = min(len(ec) for ec in strategy_equity_curves)
    portfolio_curve = []

    for i in range(min_len):
        total = sum(ec[i] for ec in strategy_equity_curves)
        portfolio_curve.append(total)

    # Add any unallocated cash
    unallocated = initial_equity * (1.0 - total_alloc)
    portfolio_curve = [p + unallocated for p in portfolio_curve]

    # Portfolio metrics
    final_equity = portfolio_curve[-1] if portfolio_curve else initial_equity
    total_return = (final_equity - initial_equity) / initial_equity * 100

    # Portfolio Sharpe
    if len(portfolio_curve) > 1:
        eq_arr = np.array(portfolio_curve)
        returns = np.diff(eq_arr) / eq_arr[:-1]
        returns = returns[~np.isnan(returns)]
        if len(returns) > 1 and np.std(returns) > 0:
            from rift_engine.backtest import _periods_per_year
            # Use the most common interval across strategies
            common_interval = strategy_configs[0].get("timeframe", "1h")
            periods = _periods_per_year(common_interval)
            portfolio_sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(periods))
        else:
            portfolio_sharpe = 0.0
    else:
        portfolio_sharpe = 0.0

    # Portfolio max drawdown
    eq_arr = np.array(portfolio_curve)
    peak = np.maximum.accumulate(eq_arr)
    drawdown = (eq_arr - peak) / peak
    portfolio_max_dd = float(np.min(drawdown)) * 100 if len(drawdown) > 0 else 0

    # Strategy return correlation matrix
    correlation_matrix = None
    if len(strategy_equity_curves) > 1:
        # Compute returns for each strategy
        strategy_returns = []
        for ec in strategy_equity_curves:
            arr = np.array(ec[:min_len])
            rets = np.diff(arr) / arr[:-1]
            rets = np.where(np.isnan(rets), 0, rets)
            strategy_returns.append(rets)

        corr = np.corrcoef(strategy_returns)
        names = [s.name for s in strategy_results]
        correlation_matrix = {
            "strategies": names,
            "matrix": [[round(float(corr[i][j]), 4) for j in range(len(names))] for i in range(len(names))],
        }

    total_trades = sum(sr.num_trades for sr in strategy_results)

    if on_progress:
        on_progress(100, "Portfolio backtest complete")

    return PortfolioResult(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return_pct=total_return,
        portfolio_sharpe=portfolio_sharpe,
        portfolio_max_drawdown_pct=portfolio_max_dd,
        total_trades=total_trades,
        strategy_results=strategy_results,
        portfolio_equity_curve=portfolio_curve,
        correlation_matrix=correlation_matrix,
    )
