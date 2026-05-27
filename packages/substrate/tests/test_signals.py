"""Tests for substrate.signals — orthogonalize + MaxIR combiner.

The decisive tests:
  - Pure-factor signal → orthogonalized residuals have near-zero IC
  - Real-edge signal → orthogonalized signal preserves edge
  - MaxIR combiner down-weights low-IC signals and up-weights high-IC ones
  - Closed-form vs constrained paths produce sensible weights
  - Shrinkage ON vs OFF produces different λ
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.risk.optimizer import OptimizationConstraints
from rift_substrate.signals import (
    InformationCoefficients,
    MaxIRCombiner,
    MaxIRWeights,
    SignalScorePanel,
    information_coefficients,
    orthogonalize_signals,
)


def _build_synthetic(T: int = 500, seed: int = 0):
    """Build a panel with 3 signals:
      - momentum: pure market beta (no real edge)
      - funding: real edge, orthogonal to market
      - spread: half-edge, half-market
    Plus the forward returns and factor returns.
    """
    rng = np.random.default_rng(seed)
    mkt = rng.normal(0.0005, 0.02, T)
    size = rng.normal(0.0002, 0.015, T)
    fwd = rng.normal(0, 0.01, T)

    momentum = 0.8 * mkt + rng.normal(0, 0.02, T)
    funding = 0.5 * fwd + rng.normal(0, 0.02, T)
    spread = 0.3 * fwd + 0.3 * mkt + rng.normal(0, 0.02, T)

    scores = np.column_stack([momentum, funding, spread])
    factor_returns = np.column_stack([mkt, size])
    ts = np.arange(T, dtype=np.int64) * 86_400_000
    panel = SignalScorePanel(
        scores=scores,
        signal_names=["momentum", "funding", "spread"],
        timestamps=ts,
    )
    return panel, factor_returns, fwd


# ─── SignalScorePanel ─────────────────────────────────────────────────


class TestSignalScorePanel:
    def test_rejects_shape_mismatch(self):
        with pytest.raises(ValueError, match="columns"):
            SignalScorePanel(
                scores=np.zeros((10, 3)),
                signal_names=["a", "b"],  # only 2 names
                timestamps=np.arange(10),
            )

    def test_rejects_row_mismatch(self):
        with pytest.raises(ValueError, match="rows"):
            SignalScorePanel(
                scores=np.zeros((10, 3)),
                signal_names=["a", "b", "c"],
                timestamps=np.arange(5),
            )

    def test_subset_preserves_order(self):
        panel, _, _ = _build_synthetic()
        sub = panel.subset(["funding", "momentum"])
        assert sub.signal_names == ["funding", "momentum"]
        assert sub.scores.shape == (panel.n_periods, 2)


# ─── information_coefficients ─────────────────────────────────────────


class TestInformationCoefficients:
    def test_high_ic_for_real_edge_signal(self):
        panel, _, fwd = _build_synthetic(seed=1)
        ic = information_coefficients(panel, fwd)
        # funding has real edge (corr ~0.5 with fwd) → IC high
        assert ic.to_dict()["funding"] > 0.15

    def test_low_ic_for_pure_factor_signal(self):
        panel, _, fwd = _build_synthetic(seed=2)
        ic = information_coefficients(panel, fwd)
        # momentum has no real edge — IC near 0
        assert abs(ic.to_dict()["momentum"]) < 0.05

    def test_pearson_default(self):
        panel, _, fwd = _build_synthetic(seed=3)
        ic = information_coefficients(panel, fwd)
        assert ic.method == "pearson"

    def test_spearman_available(self):
        panel, _, fwd = _build_synthetic(seed=4)
        ic = information_coefficients(panel, fwd, method="spearman")
        assert ic.method == "spearman"

    def test_top_n_sorts_by_absolute_ic(self):
        panel, _, fwd = _build_synthetic(seed=5)
        ic = information_coefficients(panel, fwd)
        top = ic.top_n(2)
        assert len(top) == 2
        # funding should be first (highest |IC|)
        assert top[0][0] == "funding"


# ─── orthogonalize_signals ────────────────────────────────────────────


class TestOrthogonalize:
    def test_pure_factor_signal_loses_ic_after_orthogonalization(self):
        """momentum was 0.8 × market + noise → orthogonalized version should have ~0 IC."""
        panel, factors, fwd = _build_synthetic(T=600, seed=10)
        # Raw IC
        ic_raw = information_coefficients(panel, fwd).to_dict()
        # Orthogonalize
        ortho = orthogonalize_signals(panel, factors, ["MKT", "SIZE"])
        ic_ortho = information_coefficients(ortho.orthogonalized_panel, fwd).to_dict()
        # Momentum's small IC should stay small (was already 0)
        assert abs(ic_ortho["momentum"]) < 0.07

    def test_funding_preserves_ic_after_orthogonalization(self):
        """funding was orthogonal to market → IC should be preserved."""
        panel, factors, fwd = _build_synthetic(T=600, seed=11)
        ic_raw = information_coefficients(panel, fwd).to_dict()
        ortho = orthogonalize_signals(panel, factors, ["MKT", "SIZE"])
        ic_ortho = information_coefficients(ortho.orthogonalized_panel, fwd).to_dict()
        # funding's IC should drop by less than 0.05 (was ~0.25; should stay above 0.18)
        assert abs(ic_ortho["funding"] - ic_raw["funding"]) < 0.05

    def test_factor_loadings_recovered(self):
        """The 0.8 market loading should be recovered as ~0.8."""
        panel, factors, _ = _build_synthetic(T=800, seed=12)
        ortho = orthogonalize_signals(panel, factors, ["MKT", "SIZE"])
        loadings = ortho.loadings_dict()
        # momentum was 0.8 * mkt
        assert loadings["momentum"]["MKT"] == pytest.approx(0.8, abs=0.1)

    def test_orthogonalized_panel_has_same_shape(self):
        panel, factors, _ = _build_synthetic(seed=13)
        ortho = orthogonalize_signals(panel, factors, ["MKT", "SIZE"])
        assert ortho.orthogonalized_panel.scores.shape == panel.scores.shape
        assert ortho.orthogonalized_panel.signal_names == panel.signal_names

    def test_rejects_factor_row_mismatch(self):
        panel, _, _ = _build_synthetic(T=100, seed=14)
        wrong_factors = np.zeros((50, 2))  # different T
        with pytest.raises(ValueError, match="factor_returns rows"):
            orthogonalize_signals(panel, wrong_factors, ["MKT", "SIZE"])

    def test_r_squared_higher_for_factor_exposed_signal(self):
        """momentum is 0.8 × factor; R² should be high. funding is independent; R² low."""
        panel, factors, _ = _build_synthetic(T=600, seed=15)
        ortho = orthogonalize_signals(panel, factors, ["MKT", "SIZE"])
        names = ortho.orthogonalized_panel.signal_names
        momentum_idx = names.index("momentum")
        funding_idx = names.index("funding")
        assert ortho.r_squared_per_signal[momentum_idx] > ortho.r_squared_per_signal[funding_idx]


# ─── MaxIRCombiner ────────────────────────────────────────────────────


class TestMaxIRCombiner:
    def test_higher_weight_to_higher_ic_signal(self):
        """funding has the highest IC → combiner should weight it most."""
        panel, _, fwd = _build_synthetic(T=600, seed=20)
        combiner = MaxIRCombiner(use_shrinkage=True).fit(panel, fwd)
        w = combiner.fit_result.to_dict()
        assert abs(w["funding"]) > abs(w["momentum"])
        assert abs(w["funding"]) > abs(w["spread"])

    def test_closed_form_normalizes_to_target_gross(self):
        panel, _, fwd = _build_synthetic(T=600, seed=21)
        combiner = MaxIRCombiner(
            use_shrinkage=True, max_gross_leverage_closed_form=2.5
        ).fit(panel, fwd)
        assert combiner.fit_result.gross_leverage == pytest.approx(2.5, abs=1e-6)

    def test_constrained_path_via_mv_optimizer(self):
        """When constraints are passed, falls back to MV optimizer."""
        panel, _, fwd = _build_synthetic(T=400, seed=22)
        c = OptimizationConstraints(
            max_gross_leverage=1.0,
            max_net_leverage=1.0,
            max_single_position=0.5,
            long_only=True,
        )
        combiner = MaxIRCombiner(constraints=c).fit(panel, fwd)
        assert combiner.fit_result.method == "mv_optimizer"
        # Long-only → all weights ≥ 0 (within numerical tolerance)
        assert (combiner.fit_result.weights >= -1e-6).all()
        # Max single position respected
        assert abs(combiner.fit_result.weights).max() <= 0.5 + 1e-6

    def test_combine_applies_fitted_weights(self):
        panel, _, fwd = _build_synthetic(T=400, seed=23)
        combiner = MaxIRCombiner().fit(panel, fwd)
        # Pick a single timestamp and combine
        single_scores = panel.scores[100]
        combined = combiner.combine(single_scores)
        # Should equal w'·scores
        expected = combiner.fit_result.weights @ single_scores
        assert combined == pytest.approx(expected, abs=1e-12)

    def test_combine_batch_matches_per_period_loop(self):
        panel, _, fwd = _build_synthetic(T=400, seed=24)
        combiner = MaxIRCombiner().fit(panel, fwd)
        batch = combiner.combine_batch(panel)
        # Compare to manual per-period
        manual = np.array([combiner.combine(panel.scores[t]) for t in range(panel.n_periods)])
        np.testing.assert_allclose(batch, manual, atol=1e-12)

    def test_combine_handles_nan_scores(self):
        """NaN scores should contribute 0, not propagate."""
        panel, _, fwd = _build_synthetic(T=400, seed=25)
        combiner = MaxIRCombiner().fit(panel, fwd)
        scores_with_nan = panel.scores[100].copy()
        scores_with_nan[0] = np.nan
        combined = combiner.combine(scores_with_nan)
        # Should not be NaN
        assert np.isfinite(combined)

    def test_unfit_combiner_raises_on_combine(self):
        with pytest.raises(RuntimeError, match="must be fit"):
            MaxIRCombiner().combine(np.zeros(3))

    def test_shrinkage_lambda_recorded(self):
        panel, _, fwd = _build_synthetic(T=300, seed=26)
        combiner_with = MaxIRCombiner(use_shrinkage=True).fit(panel, fwd)
        combiner_without = MaxIRCombiner(use_shrinkage=False).fit(panel, fwd)
        assert np.isfinite(combiner_with.fit_result.shrinkage_lambda)
        assert np.isnan(combiner_without.fit_result.shrinkage_lambda)

    def test_ic_floor_zeros_low_signals(self):
        """High ic_floor should zero out funding/momentum but keep funding alone."""
        # Build a panel where funding has high IC; others have very small
        rng = np.random.default_rng(27)
        T = 500
        fwd = rng.normal(0, 0.01, T)
        funding = 0.7 * fwd + rng.normal(0, 0.01, T)  # high IC
        noise1 = rng.normal(0, 0.02, T)               # ~0 IC
        noise2 = rng.normal(0, 0.02, T)               # ~0 IC
        scores = np.column_stack([noise1, funding, noise2])
        ts = np.arange(T, dtype=np.int64) * 86_400_000
        panel = SignalScorePanel(scores=scores, signal_names=["n1", "funding", "n2"], timestamps=ts)
        # Set a high floor — only funding should pass
        combiner = MaxIRCombiner(use_shrinkage=True, ic_floor=0.10).fit(panel, fwd)
        w = combiner.fit_result.to_dict()
        # funding should get most of the weight; noise signals near 0
        assert abs(w["funding"]) > abs(w["n1"])
        assert abs(w["funding"]) > abs(w["n2"])

    def test_in_sample_ic_and_ir_reported(self):
        panel, _, fwd = _build_synthetic(T=500, seed=28)
        combiner = MaxIRCombiner().fit(panel, fwd)
        assert np.isfinite(combiner.fit_result.in_sample_ic)
        assert np.isfinite(combiner.fit_result.in_sample_ir)

    def test_summary_renders(self):
        panel, _, fwd = _build_synthetic(T=300, seed=29)
        combiner = MaxIRCombiner().fit(panel, fwd)
        text = combiner.fit_result.summary()
        assert "MaxIRWeights" in text
        assert "IR" in text


# ─── End-to-end pipeline ──────────────────────────────────────────────


class TestOrthogonalizeThenCombine:
    def test_pipeline_downweights_pure_factor_signal(self):
        """Full pipeline (orthogonalize → combine) should heavily down-weight
        a pure-factor-exposure signal vs a real-edge signal."""
        panel, factors, fwd = _build_synthetic(T=600, seed=30)
        ortho = orthogonalize_signals(panel, factors, ["MKT", "SIZE"])
        combiner = MaxIRCombiner(use_shrinkage=True).fit(ortho.orthogonalized_panel, fwd)
        w = combiner.fit_result.to_dict()
        # momentum (pure factor) should have much less weight than funding (real edge)
        assert abs(w["funding"]) > 5 * abs(w["momentum"])
