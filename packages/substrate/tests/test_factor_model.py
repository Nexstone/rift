"""Tests for substrate.risk.factor_model — FactorModel + DecompositionResult.

The decisive tests are the RECOVERY tests:
  - No-edge: pure factor exposure → alpha t-stat near 0
  - Edge:    factor exposure + alpha → alpha recovered, beta recovered
  - Robust:  outliers don't bias the Huber path

The factor model is the foundation for Phase 3 (signal recombination),
Phase 2c (risk), and Phase 2d (attribution). If decomposition is wrong,
everything downstream is wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.risk import (
    DecompositionResult,
    FactorModel,
    MarketFactor,
    ReturnsPanel,
)


def _build_market_panel(
    T: int = 250,
    N: int = 20,
    seed: int = 0,
    market_mean: float = 0.0005,
    market_vol: float = 0.02,
    idio_vol: float = 0.01,
    beta_mean: float = 1.0,
    beta_sd: float = 0.3,
) -> tuple[ReturnsPanel, np.ndarray]:
    """Build a panel where each coin's return is beta_i * market + idio_i.

    Returns (panel, market_returns) so tests can reconstruct strategy
    returns with known factor structure.
    """
    rng = np.random.default_rng(seed)
    market_ret = rng.normal(market_mean, market_vol, T)
    betas = rng.normal(beta_mean, beta_sd, N)
    idio = rng.normal(0, idio_vol, (T, N))
    returns = market_ret[:, None] * betas[None, :] + idio
    volumes = rng.lognormal(15, 1, (T, N))
    timestamps = np.arange(T, dtype=np.int64) * 86_400_000
    panel = ReturnsPanel(
        returns=returns,
        coins=[f"C{i}" for i in range(N)],
        timestamps=timestamps,
        volumes=volumes,
    )
    return panel, market_ret


# ─── Construction ─────────────────────────────────────────────────────


class TestFromPanel:
    def test_default_factors(self):
        panel, _ = _build_market_panel(T=100, seed=1)
        model = FactorModel.from_panel(panel)
        assert model.factor_names == ["MKT", "SMB", "UMD"]
        assert model.factor_returns_panel().shape == (100, 3)

    def test_custom_factor_set(self):
        panel, _ = _build_market_panel(T=100, seed=2)
        model = FactorModel.from_panel(panel, factors=[MarketFactor()])
        assert model.factor_names == ["MKT"]
        assert model.factor_returns_panel().shape == (100, 1)

    def test_periods_per_year_carried_through(self):
        panel, _ = _build_market_panel(T=100, seed=3)
        model = FactorModel.from_panel(panel, periods_per_year=8760)
        assert model.periods_per_year == 8760


# ─── Decompose: recovery tests ────────────────────────────────────────


class TestDecomposeRecovery:
    def test_no_edge_strategy_yields_zero_alpha(self):
        """Strategy = 0.8 * market + iid noise. True alpha = 0."""
        panel, mkt_ret = _build_market_panel(T=300, seed=10)
        model = FactorModel.from_panel(panel, periods_per_year=365)
        rng = np.random.default_rng(100)
        strategy = 0.8 * mkt_ret + rng.normal(0, 0.005, 300)
        result = model.decompose(strategy)
        # Alpha should be statistically indistinguishable from 0
        assert abs(result.alpha_tstat) < 2.0, (
            f"no-edge alpha t-stat too large: {result.alpha_tstat:+.2f}"
        )
        # MKT loading should be near 0.8
        np.testing.assert_allclose(result.loadings["MKT"], 0.8, atol=0.1)
        # R² should be high — factors explain most of the variance
        assert result.r_squared > 0.8

    def test_true_alpha_recovered(self):
        """Strategy = +0.002/period alpha + 0.8 * market + noise."""
        panel, mkt_ret = _build_market_panel(T=300, seed=11)
        model = FactorModel.from_panel(panel, periods_per_year=365)
        rng = np.random.default_rng(101)
        true_alpha = 0.002
        strategy = true_alpha + 0.8 * mkt_ret + rng.normal(0, 0.005, 300)
        result = model.decompose(strategy)
        # Alpha estimate should be statistically significant
        assert result.alpha_tstat > 2.0, (
            f"true-alpha t-stat too small: {result.alpha_tstat:+.2f}"
        )
        # Within ~0.001 of truth (noise allows some wiggle)
        np.testing.assert_allclose(result.alpha, true_alpha, atol=0.001)
        # Annualization works: alpha * 365
        np.testing.assert_allclose(
            result.alpha_annualized, result.alpha * 365, atol=1e-9
        )

    def test_pure_idio_yields_zero_loadings(self):
        """A return series uncorrelated with all factors → loadings near 0."""
        panel, _ = _build_market_panel(T=300, seed=12)
        model = FactorModel.from_panel(panel)
        rng = np.random.default_rng(102)
        # Pure idiosyncratic noise
        strategy = rng.normal(0, 0.01, 300)
        result = model.decompose(strategy)
        for name in result.factor_names:
            assert abs(result.loading_tstats[name]) < 3.0, (
                f"pure-idio loading on {name} should be ~0; got t={result.loading_tstats[name]}"
            )
        # R² should be small
        assert result.r_squared < 0.15

    def test_robust_recovery_with_outliers(self):
        """Huber path resists return outliers that bias OLS."""
        panel, mkt_ret = _build_market_panel(T=300, seed=13)
        model = FactorModel.from_panel(panel, periods_per_year=365)
        rng = np.random.default_rng(103)
        true_alpha = 0.0
        strategy = true_alpha + 0.5 * mkt_ret + rng.normal(0, 0.005, 300)
        # Inject one big-positive outlier
        strategy[100] += 0.50
        # OLS path: outlier biases alpha upward
        result_ols = model.decompose(strategy, use_robust=False)
        # Huber path: pulls alpha back toward 0
        result_huber = model.decompose(strategy, use_robust=True)
        # Huber should be closer to true alpha (0)
        assert abs(result_huber.alpha) < abs(result_ols.alpha)


# ─── Decompose: alignment / edge cases ────────────────────────────────


class TestDecomposeAlignment:
    def test_misaligned_length_raises_without_timestamps(self):
        panel, _ = _build_market_panel(T=200, seed=20)
        model = FactorModel.from_panel(panel)
        with pytest.raises(ValueError, match="length"):
            model.decompose(np.zeros(150))  # mismatched

    def test_timestamp_inner_join(self):
        """Pass timestamps and a partial overlapping series — should align."""
        panel, mkt_ret = _build_market_panel(T=200, seed=21)
        model = FactorModel.from_panel(panel)
        # Strategy covers timestamps 50-150 of the panel
        ts_strat = panel.timestamps[50:150].copy()
        strategy = 1.0 * mkt_ret[50:150] + np.random.default_rng(0).normal(0, 0.005, 100)
        result = model.decompose(strategy, timestamps=ts_strat)
        # Should have used about 100 observations (minus NaN drops from factor warmup)
        # Just check that it returned something meaningful
        assert result.n_obs > 0
        assert "OLS+NW" in result.method or "Huber+NW" in result.method

    def test_no_timestamp_overlap_raises(self):
        panel, _ = _build_market_panel(T=100, seed=22)
        model = FactorModel.from_panel(panel)
        far_future = panel.timestamps + 10**12
        with pytest.raises(ValueError, match="overlapping"):
            model.decompose(np.zeros(100), timestamps=far_future)

    def test_drops_nan_rows(self):
        panel, mkt_ret = _build_market_panel(T=200, seed=23)
        model = FactorModel.from_panel(panel)
        strategy = 0.5 * mkt_ret + np.random.default_rng(0).normal(0, 0.005, 200)
        # Inject NaN every 20th period
        strategy[::20] = np.nan
        result = model.decompose(strategy)
        # n_obs should be less than 200 (and less than the valid factor window)
        assert 0 < result.n_obs < 200


# ─── Empty / degenerate ───────────────────────────────────────────────


class TestEmptyResult:
    def test_short_input_returns_empty_result(self):
        # Build a panel with very few periods → factors all NaN → degenerate decomposition
        panel, _ = _build_market_panel(T=15, seed=30)
        model = FactorModel.from_panel(panel)
        rng = np.random.default_rng(0)
        strategy = rng.normal(0, 0.01, 15)
        result = model.decompose(strategy)
        # Either succeeds with very few obs OR returns insufficient_data
        if result.n_obs < 8:
            assert result.method == "insufficient_data"

    def test_summary_renders_without_crashing(self):
        panel, _ = _build_market_panel(T=200, seed=31)
        model = FactorModel.from_panel(panel)
        rng = np.random.default_rng(0)
        result = model.decompose(rng.normal(0, 0.01, 200))
        text = result.summary()
        assert "DecompositionResult" in text
        assert "Alpha" in text


# ─── Reproducibility ──────────────────────────────────────────────────


class TestReproducibility:
    def test_same_panel_same_result(self):
        """Two FactorModels built from the same panel produce identical decompositions."""
        panel, mkt_ret = _build_market_panel(T=250, seed=40)
        m1 = FactorModel.from_panel(panel)
        m2 = FactorModel.from_panel(panel)
        rng = np.random.default_rng(0)
        strategy = 0.5 * mkt_ret + rng.normal(0, 0.005, 250)
        r1 = m1.decompose(strategy)
        r2 = m2.decompose(strategy)
        assert r1.alpha == r2.alpha
        assert r1.loadings == r2.loadings
        assert r1.r_squared == r2.r_squared


# ─── DecompositionResult dataclass ────────────────────────────────────


class TestDecompositionResult:
    def test_fields_present_after_real_decomposition(self):
        panel, mkt_ret = _build_market_panel(T=200, seed=50)
        model = FactorModel.from_panel(panel)
        rng = np.random.default_rng(0)
        result = model.decompose(0.5 * mkt_ret + rng.normal(0, 0.005, 200))
        # All required fields populated
        assert isinstance(result.alpha, float)
        assert isinstance(result.r_squared, float)
        assert set(result.loadings.keys()) == {"MKT", "SMB", "UMD"}
        assert set(result.loading_tstats.keys()) == {"MKT", "SMB", "UMD"}
        assert len(result.residuals) > 0
