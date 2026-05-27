"""PnL Attribution — decompose returns into alpha, beta, funding, and execution costs.

Uses the same factor decomposition approach as health.py but produces
dollar-denominated attribution for reporting rather than decay detection.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from rift_engine.tca import BUILDER_FEE_RATE


@dataclass
class PnLAttribution:
    """Complete P&L attribution breakdown."""
    # Totals
    total_pnl: float = 0.0
    initial_equity: float = 0.0
    final_equity: float = 0.0

    # Components (dollar amounts)
    alpha_pnl: float = 0.0        # pure strategy edge
    beta_pnl: float = 0.0         # market exposure return
    funding_pnl: float = 0.0      # net funding income
    execution_cost: float = 0.0   # slippage + fees (negative)
    slippage_cost: float = 0.0    # slippage only (negative)
    fee_cost: float = 0.0         # builder fees only (negative)

    # Percentages of total P&L
    alpha_pct: float = 0.0
    beta_pct: float = 0.0
    funding_pct: float = 0.0
    execution_pct: float = 0.0

    # Factor decomposition stats
    beta_coefficient: float = 0.0  # market beta
    r_squared: float = 0.0        # how much return is explained by market
    market_return_pct: float = 0.0 # benchmark (buy & hold) return

    # Per-strategy breakdown (for portfolio)
    strategy_attribution: list[dict] = field(default_factory=list)

    num_trades: int = 0


def attribute_pnl(
    trades: list[dict],
    market_prices: list[float] | None = None,
    initial_equity: float = 10000,
) -> PnLAttribution:
    """Decompose P&L into alpha, beta, funding, and execution costs.

    Args:
        trades: List of trade dicts from session logs (with TCA fields)
        market_prices: Close prices for the asset over the trading period.
                      Used for beta decomposition. If None, beta is estimated
                      from entry/exit prices.
        initial_equity: Starting equity for percentage calculations
    """
    attr = PnLAttribution()
    attr.initial_equity = initial_equity
    attr.num_trades = len(trades)

    if not trades:
        return attr

    # 1. Funding P&L — already decomposed in trade data
    attr.funding_pnl = sum(t.get("funding", t.get("funding_collected", 0)) for t in trades)

    # 2. Execution costs
    total_notional = 0.0
    for t in trades:
        entry_price = t.get("entry_price", 0)
        exit_price = t.get("exit_price", 0)
        size = t.get("size", 0)
        entry_mid = t.get("entry_mid_price", 0)
        exit_mid = t.get("exit_mid_price", 0)

        trade_notional = size * (entry_price + exit_price)
        total_notional += trade_notional

        # Slippage cost
        if entry_mid > 0:
            attr.slippage_cost -= abs(entry_price - entry_mid) * size
        if exit_mid > 0:
            attr.slippage_cost -= abs(exit_price - exit_mid) * size

    # Fee cost
    attr.fee_cost = -(total_notional * BUILDER_FEE_RATE)
    attr.execution_cost = attr.slippage_cost + attr.fee_cost

    # 3. Gross price P&L (before execution costs)
    gross_price_pnl = sum(t.get("price_pnl", t.get("pnl", 0) - t.get("funding", 0)) for t in trades)

    # 4. Alpha/Beta decomposition
    # Compute per-trade returns and market returns
    trade_returns: list[float] = []
    market_returns: list[float] = []

    if market_prices and len(market_prices) >= 2:
        # Use provided market prices for proper decomposition
        market_start = market_prices[0]
        market_end = market_prices[-1]
        attr.market_return_pct = ((market_end - market_start) / market_start * 100) if market_start > 0 else 0

        # Build per-trade strategy returns
        equity = initial_equity
        for t in trades:
            pnl = t.get("price_pnl", t.get("pnl", 0) - t.get("funding", 0))
            ret = pnl / equity if equity > 0 else 0
            trade_returns.append(ret)
            equity += pnl

        # Estimate per-trade market returns from entry/exit prices
        for t in trades:
            entry = t.get("entry_price", 0)
            exit_p = t.get("exit_price", 0)
            if entry > 0:
                mkt_ret = (exit_p - entry) / entry
                market_returns.append(mkt_ret)
            else:
                market_returns.append(0)

    else:
        # No market prices — estimate from trade prices
        equity = initial_equity
        for t in trades:
            pnl = t.get("price_pnl", t.get("pnl", 0) - t.get("funding", 0))
            ret = pnl / equity if equity > 0 else 0
            trade_returns.append(ret)
            equity += pnl

            entry = t.get("entry_price", 0)
            exit_p = t.get("exit_price", 0)
            if entry > 0:
                mkt_ret = (exit_p - entry) / entry
                market_returns.append(mkt_ret)
            else:
                market_returns.append(0)

    # Run linear regression: strategy_return = alpha + beta * market_return
    if len(trade_returns) >= 3 and len(market_returns) >= 3:
        sr = np.array(trade_returns)
        mr = np.array(market_returns)

        # Filter out any NaN/inf
        mask = np.isfinite(sr) & np.isfinite(mr)
        sr = sr[mask]
        mr = mr[mask]

        if len(sr) >= 3:
            try:
                beta, alpha = np.polyfit(mr, sr, 1)
                attr.beta_coefficient = float(beta)

                # R-squared
                ss_res = np.sum((sr - (alpha + beta * mr)) ** 2)
                ss_tot = np.sum((sr - np.mean(sr)) ** 2)
                attr.r_squared = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0

                # Dollar attribution
                # Beta P&L = what you'd have earned from pure market exposure
                total_market_return = float(np.sum(mr))
                attr.beta_pnl = beta * total_market_return * initial_equity

                # Alpha P&L = gross price P&L - beta P&L
                attr.alpha_pnl = gross_price_pnl - attr.beta_pnl
            except (np.linalg.LinAlgError, ValueError):
                attr.alpha_pnl = gross_price_pnl
                attr.beta_pnl = 0
    else:
        # Not enough trades for regression
        attr.alpha_pnl = gross_price_pnl
        attr.beta_pnl = 0

    # 5. Total P&L = alpha + beta + funding + execution
    attr.total_pnl = attr.alpha_pnl + attr.beta_pnl + attr.funding_pnl + attr.execution_cost
    attr.final_equity = initial_equity + attr.total_pnl

    # 6. Percentages
    denom = abs(attr.alpha_pnl) + abs(attr.beta_pnl) + abs(attr.funding_pnl) + abs(attr.execution_cost)
    if denom > 0:
        attr.alpha_pct = round(attr.alpha_pnl / denom * 100, 1)
        attr.beta_pct = round(attr.beta_pnl / denom * 100, 1)
        attr.funding_pct = round(attr.funding_pnl / denom * 100, 1)
        attr.execution_pct = round(attr.execution_cost / denom * 100, 1)

    # Round dollar values
    attr.total_pnl = round(attr.total_pnl, 2)
    attr.alpha_pnl = round(attr.alpha_pnl, 2)
    attr.beta_pnl = round(attr.beta_pnl, 2)
    attr.funding_pnl = round(attr.funding_pnl, 2)
    attr.execution_cost = round(attr.execution_cost, 2)
    attr.slippage_cost = round(attr.slippage_cost, 2)
    attr.fee_cost = round(attr.fee_cost, 2)
    attr.final_equity = round(attr.final_equity, 2)
    attr.beta_coefficient = round(attr.beta_coefficient, 4)
    attr.r_squared = round(attr.r_squared, 4)
    attr.market_return_pct = round(attr.market_return_pct, 2)

    return attr


def attribute_session_log(log_path: str) -> PnLAttribution:
    """Run attribution from a saved session log."""
    path = Path(log_path)
    if not path.exists():
        return PnLAttribution()
    data = json.loads(path.read_text())
    trades = data.get("trades", [])
    initial = data.get("initial_equity", 10000)
    return attribute_pnl(trades, initial_equity=initial)


def attribute_all_sessions(sessions_dir: str = "") -> PnLAttribution:
    """Run attribution across all saved session logs."""
    d = Path(sessions_dir) if sessions_dir else Path.home() / ".rift" / "algo_sessions"
    if not d.exists():
        return PnLAttribution()
    all_trades: list[dict] = []
    initial = 10000
    for f in sorted(d.glob("LIVE_*.json")):
        try:
            data = json.loads(f.read_text())
            all_trades.extend(data.get("trades", []))
            initial = data.get("initial_equity", initial)
        except Exception:
            pass
    return attribute_pnl(all_trades, initial_equity=initial)
