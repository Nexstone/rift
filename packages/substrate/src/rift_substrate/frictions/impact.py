"""Market impact models — sqrt-law baseline + empirical fitter.

Two stances:

**Sqrt-law (Almgren et al., Tóth et al. 2011)** — the universal empirical
result that price impact scales with the square root of participation rate:

    I(v) = γ · σ_daily · sqrt(v / ADV)

where γ ≈ 0.5-1.0 empirically across equity markets. The sqrt-law is the
"if you have no data, use this" baseline. It's a strong prior — universal
across asset classes, decades, and venues — but the coefficient γ varies.

**Empirical fitter** — when you have a sample of (participation_rate,
observed_slippage_bps) pairs from your own fills, fit a power-law
I(v) = a · v^b on log-log scale. Crypto-specific empirical work
(Imperial College 2024+, crypto-market-impact research) finds the sqrt-law
does NOT hold uniformly on HL-class venues — fitted exponents are flatter
or steeper depending on volume regime and time of day. The fitter lets
you discover the right shape from your own execution history rather than
pre-committing to a parametric form.

Recommended workflow:
  - Pre-trade UX before you have fills:           use sqrt-law with γ=0.7
  - After ~50 fills:                              fit `EmpiricalImpactFitter`
  - After ~500 fills, separate by time-of-day:    fit per regime

This module exposes both; `frictions.cost.estimate_trade_cost()` accepts
either as its impact model.

References:
  Tóth, B. et al. (2011). "Anomalous price impact and the critical nature
    of liquidity in financial markets." Physical Review X 1, 021006.
  Almgren, R. et al. (2005). "Direct estimation of equity market impact."
    Risk 18(7), 58-62.
  Frazzini, A., Israel, R., Moskowitz, T. (2018). "Trading Costs." NBER 23288.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


# ─── Sqrt-law functional form ─────────────────────────────────────────


def sqrt_law_impact_bps(
    trade_size_usd: float,
    adv_usd: float,
    daily_vol: float,
    gamma: float = 0.7,
) -> float:
    """Impact in bps under the square-root law.

    Args:
      trade_size_usd: $ size of the trade
      adv_usd:        average daily $ volume for this asset
      daily_vol:      daily volatility (fractional; e.g., 0.025 for 2.5% daily)
      gamma:          coefficient (default 0.7 — middle of empirical range)

    Returns:
      Expected price impact in bps.

    Edge cases:
      - trade_size_usd ≤ 0 → 0 bps (no trade, no impact)
      - adv_usd ≤ 0 → NaN (undefined)
    """
    if trade_size_usd <= 0:
        return 0.0
    if adv_usd <= 0 or not np.isfinite(adv_usd):
        return float("nan")
    participation = trade_size_usd / adv_usd
    return float(gamma * daily_vol * np.sqrt(participation) * 10_000.0)


# ─── ImpactModel ABC ──────────────────────────────────────────────────


class ImpactModel(ABC):
    """Predicts impact in bps from a trade size + market context.

    Implementations:
      - `SqrtLawImpact`           — pure-function wrapper around sqrt_law_impact_bps
      - `EmpiricalImpactFitter`   — power-law fit from observed fills
    """

    name: str = "impact_model"

    @abstractmethod
    def predict_bps(
        self,
        trade_size_usd: float,
        adv_usd: float,
        daily_vol: float,
    ) -> float:
        """Predicted impact in bps for the given trade context."""
        raise NotImplementedError


@dataclass(frozen=True)
class SqrtLawImpact(ImpactModel):
    """Sqrt-law impact model with a fixed coefficient."""

    name: str = "sqrt_law"
    gamma: float = 0.7

    def predict_bps(
        self,
        trade_size_usd: float,
        adv_usd: float,
        daily_vol: float,
    ) -> float:
        return sqrt_law_impact_bps(trade_size_usd, adv_usd, daily_vol, self.gamma)


# ─── Empirical fitter ────────────────────────────────────────────────


@dataclass
class EmpiricalImpactFitter(ImpactModel):
    """Power-law impact fit from observed fills.

    Model: I(v) = a · v^b   where v = trade_size_usd / adv_usd (participation rate)
                            and I is in bps.

    Fit via log-log linear regression of |observed slippage| on participation.

    After `.fit(participations, slippages_bps)`:
      `.a` and `.b` hold the fitted parameters
      `.predict_bps(...)` returns predictions for new trades
      `.n_samples` is the number of (filtered) data points used

    Constructor leaves `a, b` as None until `.fit()` is called. Calling
    `.predict_bps()` on an unfitted instance returns NaN.

    Crypto note: the fit may produce b far from 0.5 (the sqrt-law value).
    That's expected — sqrt-law is an equity-market result; crypto perps can
    exhibit different scaling. Inspect `b` after fitting: 0.4-0.6 means
    sqrt-law-like; <0.4 means flatter (smaller impact at higher participation);
    >0.6 means steeper (impact compounds faster).
    """

    name: str = "empirical_power"
    a: float | None = None
    b: float | None = None
    n_samples: int = 0
    r_squared: float = float("nan")

    def fit(
        self,
        participation_rates: NDArray | list[float],
        slippages_bps: NDArray | list[float],
    ) -> "EmpiricalImpactFitter":
        """Fit I(v) = a · v^b. Returns self for chaining.

        Both inputs must have the same length. Negative slippages are taken
        in absolute value (we model the magnitude of impact, signed by trade
        side at use time). Non-finite or non-positive values are dropped.
        """
        p = np.asarray(participation_rates, dtype=np.float64).ravel()
        s = np.asarray(slippages_bps, dtype=np.float64).ravel()
        if p.size != s.size:
            raise ValueError(f"length mismatch: participations={p.size}, slippages={s.size}")

        mask = np.isfinite(p) & np.isfinite(s) & (p > 0) & (np.abs(s) > 0)
        p_clean = p[mask]
        s_clean = np.abs(s[mask])

        if p_clean.size < 5:
            # Refuse to fit on too little data; leave the model unfitted.
            self.a = None
            self.b = None
            self.n_samples = int(p_clean.size)
            self.r_squared = float("nan")
            return self

        log_p = np.log(p_clean)
        log_s = np.log(s_clean)

        # OLS: log_s = log(a) + b * log_p
        # Use numpy's lstsq for stability.
        X = np.column_stack([np.ones_like(log_p), log_p])
        beta, residuals_sse, _rank, _sv = np.linalg.lstsq(X, log_s, rcond=None)
        log_a, b = float(beta[0]), float(beta[1])

        # R² (on log-log space — the residuals are log-scale)
        fitted = X @ beta
        ss_res = float(np.sum((log_s - fitted) ** 2))
        ss_tot = float(np.sum((log_s - log_s.mean()) ** 2))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

        self.a = float(np.exp(log_a))
        self.b = b
        self.n_samples = int(p_clean.size)
        self.r_squared = r_squared
        return self

    def predict_bps(
        self,
        trade_size_usd: float,
        adv_usd: float,
        daily_vol: float,  # unused — kept for ImpactModel interface
    ) -> float:
        """Predict impact in bps via the fitted power law.

        `daily_vol` is accepted but unused — the empirical fit subsumes any
        volatility dependence that was present in the calibration sample.
        Callers should refit periodically (or per regime) if conditions change.
        """
        if self.a is None or self.b is None:
            return float("nan")
        if trade_size_usd <= 0:
            return 0.0
        if adv_usd <= 0 or not np.isfinite(adv_usd):
            return float("nan")
        participation = trade_size_usd / adv_usd
        return float(self.a * (participation ** self.b))
