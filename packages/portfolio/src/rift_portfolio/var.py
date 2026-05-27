"""Value at Risk (VaR) computation for RIFT.

Three methods:
1. Historical VaR — sort returns, take percentile (no distribution assumption)
2. Parametric VaR — assumes normal distribution (fast but wrong for crypto)
3. Cornish-Fisher VaR — adjusts for skewness and kurtosis (best for crypto)

Plus CVaR (Expected Shortfall) — average loss in the tail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class VaRReport:
    """Value at Risk results."""
    # Core VaR in dollars
    var_95: float = 0.0
    var_99: float = 0.0
    var_95_pct: float = 0.0
    var_99_pct: float = 0.0

    # Method breakdown (all at 95%)
    historical_var_95: float = 0.0
    parametric_var_95: float = 0.0
    cornish_fisher_var_95: float = 0.0

    # Conditional VaR (Expected Shortfall)
    cvar_95: float = 0.0       # average loss given you're in worst 5%
    cvar_95_pct: float = 0.0

    # Context
    horizon: str = "24h"
    equity: float = 0.0
    num_observations: int = 0

    # Distribution stats
    mean_return: float = 0.0
    std_return: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0


def compute_var(
    equity_curve: list[float] | None = None,
    returns: list[float] | None = None,
    equity: float = 0.0,
    horizon: str = "24h",
    confidence: float = 0.95,
) -> VaRReport:
    """Compute Value at Risk from equity curve or returns.

    Args:
        equity_curve: List of equity values over time
        returns: Pre-computed returns (alternative to equity_curve)
        equity: Current portfolio equity (for dollar VaR)
        horizon: "1h", "24h", "7d" — scales VaR by sqrt of time
        confidence: VaR confidence level (0.95 or 0.99)
    """
    report = VaRReport(horizon=horizon, equity=equity)

    # Compute returns from equity curve if needed
    if returns is None and equity_curve is not None and len(equity_curve) >= 3:
        eq = np.array(equity_curve, dtype=float)
        returns_arr = np.diff(eq) / eq[:-1]
        returns_arr = returns_arr[np.isfinite(returns_arr)]
    elif returns is not None:
        returns_arr = np.array(returns, dtype=float)
        returns_arr = returns_arr[np.isfinite(returns_arr)]
    else:
        return report

    if len(returns_arr) < 5:
        return report

    report.num_observations = len(returns_arr)

    # Distribution statistics
    mean_r = float(np.mean(returns_arr))
    std_r = float(np.std(returns_arr, ddof=1))
    report.mean_return = round(mean_r, 6)
    report.std_return = round(std_r, 6)

    # Skewness and kurtosis (excess)
    if std_r > 0:
        skew = float(np.mean(((returns_arr - mean_r) / std_r) ** 3))
        kurt = float(np.mean(((returns_arr - mean_r) / std_r) ** 4) - 3)
    else:
        skew = 0.0
        kurt = 0.0
    report.skewness = round(skew, 4)
    report.kurtosis = round(kurt, 4)

    # Horizon scaling (square root of time rule)
    horizon_multipliers = {
        "1h": 1.0,
        "4h": 2.0,
        "24h": np.sqrt(24),
        "7d": np.sqrt(24 * 7),
        "30d": np.sqrt(24 * 30),
    }
    h = horizon_multipliers.get(horizon, np.sqrt(24))

    # Z-scores for confidence levels
    from scipy.stats import norm
    z_95 = norm.ppf(1 - 0.95)  # -1.645
    z_99 = norm.ppf(1 - 0.99)  # -2.326

    if equity <= 0:
        # Try to infer from equity curve
        if equity_curve:
            equity = equity_curve[-1]
        else:
            equity = 10000  # fallback

    report.equity = equity

    # ─── 1. Historical VaR ───
    sorted_returns = np.sort(returns_arr)
    idx_95 = int(len(sorted_returns) * 0.05)
    idx_99 = int(len(sorted_returns) * 0.01)
    hist_var_95_pct = float(sorted_returns[max(0, idx_95)]) * h
    report.historical_var_95 = round(abs(hist_var_95_pct) * equity, 2)

    # ─── 2. Parametric VaR ───
    param_var_95_pct = (mean_r + z_95 * std_r) * h
    report.parametric_var_95 = round(abs(param_var_95_pct) * equity, 2)

    # ─── 3. Cornish-Fisher VaR ───
    # Adjusted z-score for non-normal distributions
    z = z_95
    z_cf = z + (z**2 - 1) / 6 * skew + (z**3 - 3*z) / 24 * kurt - (2*z**3 - 5*z) / 36 * skew**2
    cf_var_95_pct = (mean_r + z_cf * std_r) * h
    report.cornish_fisher_var_95 = round(abs(cf_var_95_pct) * equity, 2)

    # Use Cornish-Fisher as the primary VaR (most accurate for crypto)
    report.var_95 = report.cornish_fisher_var_95
    report.var_95_pct = round(abs(cf_var_95_pct) * 100, 2)

    # 99% VaR (Cornish-Fisher)
    z99 = z_99
    z_cf_99 = z99 + (z99**2 - 1) / 6 * skew + (z99**3 - 3*z99) / 24 * kurt - (2*z99**3 - 5*z99) / 36 * skew**2
    cf_var_99_pct = (mean_r + z_cf_99 * std_r) * h
    report.var_99 = round(abs(cf_var_99_pct) * equity, 2)
    report.var_99_pct = round(abs(cf_var_99_pct) * 100, 2)

    # ─── CVaR (Expected Shortfall) ───
    # Average of all returns worse than VaR
    tail_returns = sorted_returns[:max(1, idx_95)]
    cvar_pct = float(np.mean(tail_returns)) * h
    report.cvar_95 = round(abs(cvar_pct) * equity, 2)
    report.cvar_95_pct = round(abs(cvar_pct) * 100, 2)

    return report


def var_from_sessions(sessions_dir: str = "", equity: float = 0.0) -> VaRReport:
    """Compute VaR from all algo session logs."""
    d = Path(sessions_dir) if sessions_dir else Path.home() / ".rift" / "algo_sessions"
    if not d.exists():
        return VaRReport()

    all_pnls: list[float] = []
    last_equity = equity
    for f in sorted(d.glob("LIVE_*.json")):
        try:
            data = json.loads(f.read_text())
            for t in data.get("trades", []):
                all_pnls.append(t.get("pnl", 0))
            last_equity = data.get("final_equity", last_equity)
        except Exception:
            pass

    if not all_pnls:
        return VaRReport()

    # Convert absolute P&L to returns
    eq = last_equity if last_equity > 0 else 10000
    returns = [p / eq for p in all_pnls]

    return compute_var(returns=returns, equity=eq)


def var_from_equity_curve(equity_curve: list[float], horizon: str = "24h") -> VaRReport:
    """Compute VaR from a backtest equity curve."""
    if len(equity_curve) < 5:
        return VaRReport()
    return compute_var(equity_curve=equity_curve, equity=equity_curve[-1], horizon=horizon)
