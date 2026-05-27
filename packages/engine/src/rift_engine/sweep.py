"""Parameter sweep engine.

Tests all combinations of strategy parameters and ranks results.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, fields as dc_fields
from typing import Any

import polars as pl

from rift_engine.backtest import BacktestResult, run_backtest
from rift_engine.strategy import Strategy


@dataclass
class SweepEntry:
    """A single parameter combination and its backtest result."""

    params: dict[str, Any]
    result: BacktestResult

    def to_dict(self) -> dict:
        return {
            "params": self.params,
            "metrics": self.result.to_dict(),
        }


@dataclass
class SweepResult:
    """Results from a full parameter sweep."""

    strategy_name: str
    pair: str
    interval: str
    total_combos: int
    entries: list[SweepEntry]

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_name,
            "pair": self.pair,
            "interval": self.interval,
            "total_combos": self.total_combos,
            "completed": len(self.entries),
        }

    def top_by_sharpe(self, n: int = 10) -> list[SweepEntry]:
        return sorted(self.entries, key=lambda e: e.result.sharpe_ratio, reverse=True)[:n]

    def top_by_return(self, n: int = 10) -> list[SweepEntry]:
        return sorted(self.entries, key=lambda e: e.result.total_return_pct, reverse=True)[:n]

    def top_by_profit_factor(self, n: int = 10) -> list[SweepEntry]:
        return sorted(self.entries, key=lambda e: e.result.profit_factor, reverse=True)[:n]


def parse_sweep_config(config: dict[str, Any]) -> dict[str, list[Any]]:
    """Parse sweep config (from YAML) into param name → list of values."""
    sweep_params = config.get("sweep", config.get("params", config))
    result = {}
    for key, values in sweep_params.items():
        if isinstance(values, list):
            result[key] = values
        else:
            result[key] = [values]
    return result


def generate_combinations(params: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Generate all parameter combinations from sweep config."""
    keys = list(params.keys())
    values = [params[k] for k in keys]
    combos = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


def run_sweep(
    strategy_cls: type[Strategy],
    df: pl.DataFrame,
    sweep_params: dict[str, list[Any]],
    strategy_name: str = "unknown",
    pair: str = "unknown",
    interval: str = "unknown",
    initial_equity: float = 10000.0,
    fee_rate: float = 0.00035,
    leverage: float = 1.0,
    on_progress: callable = None,
    funding_df: pl.DataFrame | None = None,
) -> SweepResult:
    """Run a parameter sweep across all combinations.

    Args:
        strategy_cls: Strategy class to instantiate with different configs
        df: Candle DataFrame
        sweep_params: Dict of param_name → list of values to test
        on_progress: Optional callback(pct, msg) for progress
    """
    combos = generate_combinations(sweep_params)
    total = len(combos)

    # Deduplicate combos (rounding can create duplicate value sets)
    seen = set()
    unique_combos = []
    for c in combos:
        key = tuple(sorted(c.items()))
        if key not in seen:
            seen.add(key)
            unique_combos.append(c)
    combos = unique_combos
    total = len(combos)

    result = SweepResult(
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        total_combos=total,
        entries=[],
    )

    import time as _time
    sweep_start = _time.time()

    for i, params in enumerate(combos):
        if on_progress:
            pct = int((i / total) * 100)
            # Show ETA after first few combos
            eta_str = ""
            if i >= 3:
                elapsed = _time.time() - sweep_start
                per_combo = elapsed / i
                remaining = per_combo * (total - i)
                if remaining > 3600:
                    eta_str = f" — ETA {remaining / 3600:.1f}h"
                elif remaining > 60:
                    eta_str = f" — ETA {remaining / 60:.0f}m"
                else:
                    eta_str = f" — ETA {remaining:.0f}s"
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            on_progress(pct, f"Combo {i+1}/{total}{eta_str}: {param_str}")

        # Build config from params
        try:
            if strategy_cls.config_class:
                # Get default values, override with sweep params
                config_fields = {f.name: f.default for f in dc_fields(strategy_cls.config_class)}
                config_fields.update(params)
                config = strategy_cls.config_class(**config_fields)
                strategy = strategy_cls(config=config)
            else:
                strategy = strategy_cls()
        except Exception:
            continue

        # Use leverage from config if available
        lev = getattr(strategy.config, 'leverage', leverage) if strategy.config else leverage

        bt_result = run_backtest(
            strategy=strategy,
            df=df,
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            initial_equity=initial_equity,
            silent=True,
            funding_df=funding_df,
            fee_rate=fee_rate,
            leverage=lev,
        )

        result.entries.append(SweepEntry(params=params, result=bt_result))

    if on_progress:
        on_progress(100, f"Sweep complete: {len(result.entries)}/{total} combinations")

    return result
