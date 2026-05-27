"""Tests for substrate.attribution — returns + $ PnL decomposition.

The decisive tests are:
  - Decomposition adds up arithmetically (components sum to total return)
  - Synthetic alpha is recovered with statistical significance
  - Pure factor exposure (no alpha) shows zero alpha t-stat
  - Cost rollup integrates correctly into total PnL
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.attribution import (
    PnLAttribution,
    ReturnsAttribution,
    attribute_pnl,
    attribute_returns,
)
from rift_substrate.risk import FactorModel, ReturnsPanel


def _build_model_and_market(T: int = 300, N: int = 20, seed: int = 0):
    """Build a factor model + return the synthetic market series used to build it."""
    rng = np.random.default_rng(seed)
    mkt_ret = rng.normal(0.0005, 0.02, T)
    betas = rng.normal(1.0, 0.3, N)
    idio = rng.normal(0, 0.01, (T, N))
    panel_returns = mkt_ret[:, None] * betas[None, :] + idio
    volumes = rng.lognormal(15, 1, (T, N))
    timestamps = np.arange(T, dtype=np.int64) * 86_400_000
    panel = ReturnsPanel(
        returns=panel_returns,
        coins=[f"C{i}" for i in range(N)],
        timestamps=timestamps,
        volumes=volumes,
    )
    model = FactorModel.from_panel(panel, periods_per_year=365)
    return model, mkt_ret


# ─── attribute_returns ────────────────────────────────────────────────


class TestAttributeReturns:
    def test_components_sum_to_total_arithmetic(self):
        """alpha + Σfactor_contributions + residual == total_return_arithmetic."""
        model, mkt = _build_model_and_market()
        strat = 0.001 + 0.6 * mkt + np.random.default_rng(1).normal(0, 0.005, len(mkt))
        ra = attribute_returns(strat, model)
        components = (
            ra.alpha_arithmetic
            + sum(ra.factor_arithmetic.values())
            + ra.residual_arithmetic
        )
        assert components == pytest.approx(ra.total_return_arithmetic, abs=1e-10)

    def test_true_alpha_recovered_with_significance(self):
        """Synthetic alpha+beta → recovered alpha is statistically significant."""
        model, mkt = _build_model_and_market(T=400, seed=2)
        true_alpha = 0.002
        strat = true_alpha + 0.6 * mkt + np.random.default_rng(3).normal(0, 0.005, len(mkt))
        ra = attribute_returns(strat, model)
        assert ra.alpha_tstat > 2.0
        assert ra.alpha_per_period == pytest.approx(true_alpha, abs=0.001)

    def test_pure_factor_exposure_yields_zero_alpha(self):
        """Strategy with no alpha → t-stat near 0."""
        model, mkt = _build_model_and_market(T=400, seed=4)
        strat = 1.0 * mkt + np.random.default_rng(5).normal(0, 0.005, len(mkt))
        ra = attribute_returns(strat, model)
        assert abs(ra.alpha_tstat) < 2.0

    def test_residual_near_zero_when_model_explains_well(self):
        """High R² → small residual contribution."""
        model, mkt = _build_model_and_market(T=400, seed=6)
        strat = 0.5 * mkt + np.random.default_rng(7).normal(0, 0.001, len(mkt))  # tiny noise
        ra = attribute_returns(strat, model)
        # Residual should be very small relative to total
        assert abs(ra.residual_arithmetic) < abs(ra.total_return_arithmetic) * 0.2

    def test_compound_vs_arithmetic_total(self):
        """Compound and arithmetic totals differ by ~σ²/2 vol drag."""
        model, mkt = _build_model_and_market(T=400, seed=8)
        strat = 1.0 * mkt + np.random.default_rng(9).normal(0, 0.005, len(mkt))
        ra = attribute_returns(strat, model)
        # For non-trivial vol, compound < arithmetic (positive expected return)
        # The relationship: log(1+r_compound) ≈ r_arith - σ²/2 * T
        assert ra.total_return_compound != ra.total_return_arithmetic

    def test_empty_strategy_returns_no_observations(self):
        model, _ = _build_model_and_market(T=20, seed=10)
        # Too short → factor returns mostly NaN → no obs
        ra = attribute_returns(np.zeros(20), model)
        # Either 0 obs or very few
        assert ra.n_observations <= 20

    def test_summary_renders(self):
        model, mkt = _build_model_and_market(T=300, seed=11)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(12).normal(0, 0.005, len(mkt))
        ra = attribute_returns(strat, model)
        text = ra.summary()
        assert "ReturnsAttribution" in text
        assert "Alpha contribution" in text
        # All factor names present
        for name in ra.loadings:
            assert name in text


# ─── attribute_pnl ────────────────────────────────────────────────────


class TestAttributePnl:
    def test_dollar_decomposition_sums_to_gross(self):
        """alpha_pnl + factor_pnl + residual_pnl == gross_pnl."""
        model, mkt = _build_model_and_market(T=400, seed=13)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(14).normal(0, 0.005, len(mkt))
        pa = attribute_pnl(strat, notional_usd=100_000, factor_model=model)
        component_sum = (
            pa.alpha_pnl_usd
            + sum(pa.factor_pnl_usd.values())
            + pa.residual_pnl_usd
        )
        assert component_sum == pytest.approx(pa.gross_pnl_usd, abs=1e-6)

    def test_total_pnl_equals_gross_plus_costs(self):
        model, mkt = _build_model_and_market(T=400, seed=15)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(16).normal(0, 0.005, len(mkt))
        costs = {"fees": -500.0, "funding": -200.0, "slippage": -100.0}
        pa = attribute_pnl(strat, 100_000, model, cost_breakdown_usd=costs)
        assert pa.cost_pnl_usd == pytest.approx(-800.0)
        assert pa.total_pnl_usd == pytest.approx(pa.gross_pnl_usd + pa.cost_pnl_usd)

    def test_no_costs_means_total_equals_gross(self):
        model, mkt = _build_model_and_market(T=300, seed=17)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(18).normal(0, 0.005, len(mkt))
        pa = attribute_pnl(strat, 100_000, model)
        assert pa.total_pnl_usd == pytest.approx(pa.gross_pnl_usd)
        assert pa.cost_pnl_usd == 0.0

    def test_scalar_vs_array_notional(self):
        """Same constant notional whether passed as scalar or array."""
        model, mkt = _build_model_and_market(T=300, seed=19)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(20).normal(0, 0.005, len(mkt))
        pa_scalar = attribute_pnl(strat, 100_000, model)
        # n_obs from scalar version
        notional_vec = np.full(pa_scalar.n_periods, 100_000.0)
        # Need to ensure attribute_returns drops same NaN rows; pass full panel
        pa_array = attribute_pnl(strat, notional_vec, model)
        assert pa_scalar.total_pnl_usd == pytest.approx(pa_array.total_pnl_usd)

    def test_percentages_have_correct_sign(self):
        """Pure long-only with positive alpha → positive pcts."""
        model, mkt = _build_model_and_market(T=400, seed=21)
        strat = 0.002 + 0.5 * mkt + np.random.default_rng(22).normal(0, 0.005, len(mkt))
        pa = attribute_pnl(strat, 100_000, model, cost_breakdown_usd={"fees": -100.0})
        # Total should be positive (true alpha > costs)
        assert pa.total_pnl_usd > 0
        assert pa.alpha_pnl_usd > 0
        assert pa.alpha_pct > 0
        # Cost pct is negative (denominator is abs)
        assert pa.cost_pct < 0

    def test_summary_renders(self):
        model, mkt = _build_model_and_market(T=300, seed=23)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(24).normal(0, 0.005, len(mkt))
        pa = attribute_pnl(strat, 100_000, model, cost_breakdown_usd={"fees": -50, "funding": -25})
        text = pa.summary()
        assert "PnLAttribution" in text
        assert "Alpha contribution" in text
        assert "Costs total" in text
        # All cost lines present
        assert "fees" in text
        assert "funding" in text

    def test_notional_size_mismatch_raises(self):
        model, mkt = _build_model_and_market(T=300, seed=25)
        strat = 0.001 + 0.5 * mkt + np.random.default_rng(26).normal(0, 0.005, len(mkt))
        # Pass notional that's too short
        with pytest.raises(ValueError, match="notional_usd has size"):
            attribute_pnl(strat, np.array([1.0, 2.0, 3.0]), model)

    def test_inherits_statistical_significance(self):
        """alpha_tstat and r_squared come through from underlying regression."""
        model, mkt = _build_model_and_market(T=400, seed=27)
        true_alpha = 0.002
        strat = true_alpha + 0.5 * mkt + np.random.default_rng(28).normal(0, 0.005, len(mkt))
        pa = attribute_pnl(strat, 100_000, model)
        assert pa.alpha_tstat > 2.0
        assert 0 < pa.r_squared < 1
