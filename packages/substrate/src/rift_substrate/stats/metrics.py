"""Core performance metrics with bootstrapped confidence intervals.

Every metric here is reported with a 95% CI by default. The IID bootstrap
is wrong for serially-dependent returns, so we use stationary block bootstrap
(see bootstrap.py) for all CI computation.

Conventions:
  - All metrics computed from a 1-D returns series (period returns, not log)
  - 'period' = the natural unit of the input series (e.g., daily, hourly)
  - Annualization factor is supplied by caller (e.g., 252 for daily,
    8760 for hourly, 365 for daily including weekends in crypto)
  - Crypto convention: use 365 for daily, 8760 for hourly, 525600 for 1m
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
from numpy.typing import NDArray

from rift_substrate.stats.bootstrap import stationary_bootstrap, optimal_block_size


# ─── Annualization helpers ─────────────────────────────────────────────


def annualization_factor(periods_per_year: int | float) -> float:
    """Conversion factor for annualizing a periodic metric."""
    return float(periods_per_year)


# Common defaults
CRYPTO_DAILY = 365      # crypto trades 7 days/wk
CRYPTO_HOURLY = 8760    # 24 * 365
CRYPTO_MINUTELY = 525600


def periods_per_year_for_interval(interval: str) -> float:
    """Map an interval string ("1m", "5m", "1h", "4h", "1d", "1w") to
    the number of periods in a calendar year (crypto convention: 365 days/yr).

    Supports any "<int><unit>" form with unit in s|m|h|d|w.
    """
    if not interval or len(interval) < 2:
        raise ValueError(f"unrecognized interval: {interval!r}")
    unit = interval[-1]
    try:
        n = int(interval[:-1])
    except ValueError as exc:
        raise ValueError(f"unrecognized interval: {interval!r}") from exc
    if n <= 0:
        raise ValueError(f"interval count must be positive: {interval!r}")
    seconds_per_year = 365.0 * 24 * 60 * 60
    seconds_per_period = {
        "s": n,
        "m": n * 60,
        "h": n * 3600,
        "d": n * 86400,
        "w": n * 86400 * 7,
    }.get(unit)
    if seconds_per_period is None:
        raise ValueError(f"unrecognized interval unit: {interval!r}")
    return seconds_per_year / seconds_per_period


# ─── Point estimates ───────────────────────────────────────────────────


def _drop_nan(arr: NDArray) -> NDArray:
    return arr[~np.isnan(arr)]


def annual_return(returns: NDArray, periods_per_year: float) -> float:
    """Compounded annual return.  ((1+r).prod())^(periods/n) - 1"""
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size == 0:
        return float("nan")
    total = float(np.prod(1.0 + r))
    if total <= 0:
        return -1.0
    return total ** (periods_per_year / r.size) - 1.0


def annual_vol(returns: NDArray, periods_per_year: float) -> float:
    """Annualized standard deviation."""
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size < 2:
        return float("nan")
    return float(np.std(r, ddof=1) * np.sqrt(periods_per_year))


def sharpe_ratio(returns: NDArray, periods_per_year: float, rf: float = 0.0) -> float:
    """Annualized Sharpe ratio.

    rf is the per-period risk-free return (e.g., 0.0001 if rf annualized
    is ~3.65% on daily data). Most crypto research uses rf=0.

    Returns NaN if returns are effectively constant (std smaller than 1e-12
    times mean magnitude, or smaller than 1e-15 absolute).
    """
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size < 2:
        return float("nan")
    excess = r - rf
    sd = float(np.std(excess, ddof=1))
    # Effectively-zero variance check (floating point safe).
    # Constant arrays give std ~1e-19 due to float error; treat as zero.
    scale = max(abs(float(np.mean(excess))), 1e-15)
    if sd <= 0 or sd < 1e-12 * scale:
        return float("nan")
    return float(np.mean(excess) / sd * np.sqrt(periods_per_year))


def sortino_ratio(returns: NDArray, periods_per_year: float, target: float = 0.0) -> float:
    """Annualized Sortino ratio (downside deviation in the denominator).

    target = minimum acceptable return per period (default 0).
    """
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size < 2:
        return float("nan")
    excess = r - target
    downside = excess[excess < 0]
    if downside.size == 0:
        return float("inf") if np.mean(excess) > 0 else float("nan")
    # Use population std on downside (standard convention)
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd <= 0:
        return float("nan")
    return float(np.mean(excess) / dd * np.sqrt(periods_per_year))


def max_drawdown(returns: NDArray) -> float:
    """Maximum drawdown as a (negative) decimal: -0.20 = -20%."""
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size == 0:
        return float("nan")
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(dd.min())


def calmar_ratio(returns: NDArray, periods_per_year: float) -> float:
    """Annual return / |max drawdown|."""
    mdd = max_drawdown(returns)
    if not np.isfinite(mdd) or mdd >= 0:
        return float("nan")
    ar = annual_return(returns, periods_per_year)
    if not np.isfinite(ar):
        return float("nan")
    return float(ar / abs(mdd))


# ─── Distribution properties ───────────────────────────────────────────


def skewness(returns: NDArray) -> float:
    """Sample skewness (Fisher's definition; normal=0)."""
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size < 3:
        return float("nan")
    mu = r.mean()
    sd = r.std(ddof=0)
    if sd <= 0:
        return float("nan")
    return float(np.mean(((r - mu) / sd) ** 3))


def kurtosis(returns: NDArray) -> float:
    """Sample kurtosis (Pearson's definition; normal=3, excess kurtosis = this - 3)."""
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size < 4:
        return float("nan")
    mu = r.mean()
    sd = r.std(ddof=0)
    if sd <= 0:
        return float("nan")
    return float(np.mean(((r - mu) / sd) ** 4))


def autocorrelation(returns: NDArray, lag: int = 1) -> float:
    """Sample autocorrelation at the given lag."""
    r = _drop_nan(np.asarray(returns, dtype=np.float64))
    if r.size <= lag:
        return float("nan")
    centered = r - r.mean()
    var = float((centered ** 2).sum() / r.size)
    if var <= 0:
        return float("nan")
    return float((centered[:-lag] * centered[lag:]).sum() / (r.size * var))


# ─── MetricBundle dataclass ────────────────────────────────────────────


@dataclass(frozen=True)
class MetricBundle:
    """All core metrics + CIs + distribution properties, bundled.

    Use Stats.from_returns() to construct.
    """
    # Sample info
    n_observations: int
    periods_per_year: float

    # Point estimates
    annual_return: float
    annual_vol: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float

    # 95% confidence intervals (stationary block bootstrap)
    annual_return_ci_95: tuple[float, float]
    annual_vol_ci_95: tuple[float, float]
    sharpe_ci_95: tuple[float, float]
    sortino_ci_95: tuple[float, float]
    max_drawdown_ci_95: tuple[float, float]

    # Distribution properties
    skew: float
    kurtosis: float
    autocorr_lag1: float

    # Inputs to PSR/DSR (caller can recompute with different thresholds)
    bootstrap_block_size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = [
            f"MetricBundle  (n={self.n_observations}, periods/yr={self.periods_per_year:g})",
            "─" * 60,
            f"  Annual return: {self.annual_return:>+8.2%}   95% CI: [{self.annual_return_ci_95[0]:+.2%}, {self.annual_return_ci_95[1]:+.2%}]",
            f"  Annual vol:    {self.annual_vol:>8.2%}    95% CI: [{self.annual_vol_ci_95[0]:.2%}, {self.annual_vol_ci_95[1]:.2%}]",
            f"  Sharpe:        {self.sharpe:>+8.3f}    95% CI: [{self.sharpe_ci_95[0]:+.3f}, {self.sharpe_ci_95[1]:+.3f}]",
            f"  Sortino:       {self.sortino:>+8.3f}    95% CI: [{self.sortino_ci_95[0]:+.3f}, {self.sortino_ci_95[1]:+.3f}]",
            f"  Calmar:        {self.calmar:>+8.3f}",
            f"  Max DD:        {self.max_drawdown:>+8.2%}   95% CI: [{self.max_drawdown_ci_95[0]:.2%}, {self.max_drawdown_ci_95[1]:.2%}]",
            "",
            f"  Skew:          {self.skew:>+8.3f}",
            f"  Kurtosis:      {self.kurtosis:>+8.3f}   (excess: {self.kurtosis - 3:+.3f})",
            f"  Autocorr(1):   {self.autocorr_lag1:>+8.3f}",
        ]
        return "\n".join(lines)


# ─── Stats entry point ─────────────────────────────────────────────────


class Stats:
    """Entry point for statistical analysis of a returns series."""

    @staticmethod
    def from_returns(
        returns: NDArray | list | tuple,
        periods_per_year: float = CRYPTO_DAILY,
        rf: float = 0.0,
        n_bootstrap: int = 1000,
        block_size: int | None = None,
        seed: int | None = None,
    ) -> MetricBundle:
        """Compute full MetricBundle from a periodic returns series.

        Args:
          returns:          1-D series of per-period returns (NOT log)
          periods_per_year: annualization factor (365 daily, 8760 hourly, ...)
          rf:               per-period risk-free rate (default 0)
          n_bootstrap:      bootstrap resamples for CIs (default 1000)
          block_size:       auto-pick if None (Politis-White)
          seed:             RNG seed for reproducibility

        Returns:
          MetricBundle with all metrics + 95% CIs + distribution properties.
        """
        r = _drop_nan(np.asarray(returns, dtype=np.float64))
        if r.size < 2:
            raise ValueError(f"need at least 2 observations; got {r.size}")

        # Point estimates
        ar = annual_return(r, periods_per_year)
        av = annual_vol(r, periods_per_year)
        sh = sharpe_ratio(r, periods_per_year, rf=rf)
        so = sortino_ratio(r, periods_per_year)
        ca = calmar_ratio(r, periods_per_year)
        mdd = max_drawdown(r)

        # Bootstrap for CIs
        if block_size is None:
            block_size = optimal_block_size(r)
        resamples = stationary_bootstrap(r, n_resamples=n_bootstrap,
                                          avg_block_size=block_size, seed=seed)

        ar_dist = np.array([annual_return(s, periods_per_year) for s in resamples])
        av_dist = np.array([annual_vol(s, periods_per_year) for s in resamples])
        sh_dist = np.array([sharpe_ratio(s, periods_per_year, rf=rf) for s in resamples])
        so_dist = np.array([sortino_ratio(s, periods_per_year) for s in resamples])
        mdd_dist = np.array([max_drawdown(s) for s in resamples])

        def _ci(dist: NDArray) -> tuple[float, float]:
            clean = dist[np.isfinite(dist)]
            if clean.size < 2:
                return (float("nan"), float("nan"))
            lo, hi = np.quantile(clean, [0.025, 0.975])
            return (float(lo), float(hi))

        return MetricBundle(
            n_observations=int(r.size),
            periods_per_year=float(periods_per_year),
            annual_return=ar,
            annual_vol=av,
            sharpe=sh,
            sortino=so,
            calmar=ca,
            max_drawdown=mdd,
            annual_return_ci_95=_ci(ar_dist),
            annual_vol_ci_95=_ci(av_dist),
            sharpe_ci_95=_ci(sh_dist),
            sortino_ci_95=_ci(so_dist),
            max_drawdown_ci_95=_ci(mdd_dist),
            skew=skewness(r),
            kurtosis=kurtosis(r),
            autocorr_lag1=autocorrelation(r, lag=1),
            bootstrap_block_size=int(block_size),
        )
