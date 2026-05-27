"""Tests for substrate.cross_impact — basket execution with correlation effects.

Pins:
  1. Zero correlation → cross_term = 0 (recovers sum of own-impacts)
  2. cross_dampening=0 → cross_term = 0 (recovers sum of own-impacts)
  3. Aligned basket (same direction + positive ρ) → cross_term > 0
  4. Hedged pair (opposite direction + positive ρ) → cross_term < 0
  5. Negative correlation flips the aligned/hedged signs
  6. Sign symmetry: flipping all trade signs preserves total_cost magnitude
  7. cross_term linear in cross_dampening
  8. Single-asset basket reduces to own-impact
  9. Zero-size trade contributes nothing
 10. correlation_matrix recovers known ρ from synthetic data
 11. Shape mismatches raise
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.cross_impact import (
    BasketImpactResult,
    basket_impact,
    correlation_matrix,
)
from rift_substrate.frictions.impact import SqrtLawImpact


# ─── Helpers ─────────────────────────────────────────────────────────


def _impact() -> SqrtLawImpact:
    return SqrtLawImpact(gamma=0.7)


def _symmetric_setup(n: int = 2):
    advs = np.full(n, 100_000_000.0)
    vols = np.full(n, 0.03)
    return advs, vols


# ─── basket_impact ───────────────────────────────────────────────────


class TestBasketImpact:
    def test_zero_correlation_eliminates_cross_term(self):
        advs, vols = _symmetric_setup()
        rho = np.eye(2)
        r = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        assert r.cross_term_usd == pytest.approx(0.0, abs=1e-9)
        assert r.total_cost_usd == pytest.approx(r.diagonal_cost_usd)

    def test_zero_dampening_eliminates_cross_term(self):
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
            cross_dampening=0.0,
        )
        assert r.cross_term_usd == pytest.approx(0.0, abs=1e-9)

    def test_aligned_basket_positive_cross_term(self):
        """Long + Long with positive ρ → cross-impact adds cost."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        assert r.cross_term_usd > 0
        assert r.total_cost_usd > r.diagonal_cost_usd

    def test_hedged_pair_negative_cross_term(self):
        """Long + Short with positive ρ → cross-impact reduces cost."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r = basket_impact(
            trades_usd=[50_000.0, -50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        assert r.cross_term_usd < 0
        assert r.total_cost_usd < r.diagonal_cost_usd

    def test_negative_correlation_flips_signs(self):
        """Long + Long with NEGATIVE ρ → cross-impact REDUCES cost
        (each long opposes the other's price pressure via inverse correlation)."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, -0.8], [-0.8, 1.0]])
        r = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        # Same-direction trades but anti-correlated assets → cross_term < 0
        assert r.cross_term_usd < 0

    def test_aligned_hedge_symmetry(self):
        """Cross-term magnitude should be the same for long+long and long-short
        under symmetric inputs (same ADV, same vol, same |ρ|)."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r_align = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        r_hedge = basket_impact(
            trades_usd=[50_000.0, -50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        # Aligned cross_term > 0; hedged cross_term < 0; same magnitude
        assert r_align.cross_term_usd == pytest.approx(
            -r_hedge.cross_term_usd, rel=1e-9
        )

    def test_total_cost_symmetric_under_sign_flip(self):
        """Flipping every trade's sign preserves the total cost magnitude
        (cost depends on |q_i| and aligned-vs-hedged structure, not absolute direction)."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r_pos = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        r_neg = basket_impact(
            trades_usd=[-50_000.0, -50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        assert r_pos.total_cost_usd == pytest.approx(r_neg.total_cost_usd, rel=1e-9)

    def test_cross_term_linear_in_dampening(self):
        """Halving cross_dampening should halve the cross_term."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r_full = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(), cross_dampening=1.0,
        )
        r_half = basket_impact(
            trades_usd=[50_000.0, 50_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(), cross_dampening=0.5,
        )
        assert r_half.cross_term_usd == pytest.approx(
            r_full.cross_term_usd / 2.0, rel=1e-9
        )

    def test_single_asset_reduces_to_own_impact(self):
        """Single-asset basket: total cost == diagonal cost == own-impact cost."""
        r = basket_impact(
            trades_usd=[100_000.0],
            correlations=np.array([[1.0]]),
            advs_usd=[100_000_000.0],
            daily_vols=[0.03],
            impact_model=_impact(),
        )
        assert r.cross_term_usd == pytest.approx(0.0, abs=1e-9)
        assert r.total_cost_usd == pytest.approx(r.diagonal_cost_usd, rel=1e-9)
        # Expected: own_impact_bps × $100K / 10000
        own_bps = _impact().predict_bps(100_000.0, 100_000_000.0, 0.03)
        expected = 100_000.0 * own_bps / 10_000.0
        assert r.total_cost_usd == pytest.approx(expected, rel=1e-9)

    def test_zero_trade_contributes_nothing(self):
        """An asset with q=0 contributes no own-impact and no cross-impact source."""
        advs, vols = _symmetric_setup()
        rho = np.array([[1.0, 0.8], [0.8, 1.0]])
        r = basket_impact(
            trades_usd=[100_000.0, 0.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        # Asset 1's impact should still receive cross-impact from asset 0,
        # but it has q=0 so no cost on asset 1 (q × impact = 0).
        assert r.impact_costs_usd[1] == pytest.approx(0.0, abs=1e-9)
        # Asset 1 still receives a non-zero impact_bps from asset 0's trade
        assert abs(r.impacts_bps[1]) > 0

    def test_asset_names_propagate(self):
        advs, vols = _symmetric_setup()
        rho = np.eye(2)
        r = basket_impact(
            trades_usd=[10_000.0, 10_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
            asset_names=["BTC", "ETH"],
        )
        assert r.asset_names == ["BTC", "ETH"]
        assert "BTC" in r.summary()

    def test_default_asset_names(self):
        advs, vols = _symmetric_setup()
        rho = np.eye(2)
        r = basket_impact(
            trades_usd=[10_000.0, 10_000.0],
            correlations=rho, advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        assert r.asset_names == ["asset_0", "asset_1"]

    def test_shape_mismatch_raises(self):
        advs, vols = _symmetric_setup()
        with pytest.raises(ValueError, match="correlations"):
            basket_impact(
                trades_usd=[10_000.0, 10_000.0],
                correlations=np.eye(3),  # wrong size
                advs_usd=advs, daily_vols=vols,
                impact_model=_impact(),
            )

    def test_dampening_out_of_range_raises(self):
        advs, vols = _symmetric_setup()
        with pytest.raises(ValueError, match="cross_dampening"):
            basket_impact(
                trades_usd=[10_000.0, 10_000.0],
                correlations=np.eye(2),
                advs_usd=advs, daily_vols=vols,
                impact_model=_impact(),
                cross_dampening=1.5,
            )

    def test_asset_names_length_mismatch_raises(self):
        advs, vols = _symmetric_setup()
        with pytest.raises(ValueError, match="asset_names"):
            basket_impact(
                trades_usd=[10_000.0, 10_000.0],
                correlations=np.eye(2),
                advs_usd=advs, daily_vols=vols,
                impact_model=_impact(),
                asset_names=["BTC", "ETH", "SOL"],
            )

    def test_returns_basketimpactresult(self):
        advs, vols = _symmetric_setup()
        r = basket_impact(
            trades_usd=[10_000.0, 10_000.0],
            correlations=np.eye(2),
            advs_usd=advs, daily_vols=vols,
            impact_model=_impact(),
        )
        assert isinstance(r, BasketImpactResult)
        s = r.summary()
        assert "BasketImpactResult" in s
        assert "Total cost" in s


# ─── correlation_matrix ──────────────────────────────────────────────


class TestCorrelationMatrix:
    def test_recovers_identity_on_independent_data(self):
        rng = np.random.default_rng(0)
        R = rng.standard_normal((5000, 3))
        rho = correlation_matrix(R)
        np.testing.assert_allclose(np.diag(rho), 1.0)
        # Off-diagonals near zero
        assert abs(rho[0, 1]) < 0.05
        assert abs(rho[0, 2]) < 0.05

    def test_recovers_high_correlation(self):
        rng = np.random.default_rng(0)
        x = rng.standard_normal(2000)
        y = 0.9 * x + 0.1 * rng.standard_normal(2000)
        R = np.column_stack([x, y])
        rho = correlation_matrix(R)
        # ρ_xy should be near corr(x, 0.9x + 0.1*noise) ≈ 0.9 / sqrt(0.81 + 0.01)
        assert rho[0, 1] > 0.95
        # Symmetry
        assert rho[0, 1] == rho[1, 0]

    def test_handles_nan_pairwise(self):
        R = np.array([
            [1.0, 2.0],
            [np.nan, 3.0],
            [3.0, 4.0],
            [4.0, np.nan],
            [5.0, 6.0],
        ])
        rho = correlation_matrix(R)
        # Should compute on pairwise complete observations (3 pairs: rows 0, 2, 4)
        assert np.isfinite(rho[0, 1])
        # Perfectly correlated on the complete pairs
        assert rho[0, 1] == pytest.approx(1.0)

    def test_diagonal_is_exactly_one(self):
        R = np.random.default_rng(0).standard_normal((100, 4))
        rho = correlation_matrix(R)
        np.testing.assert_array_equal(np.diag(rho), [1.0, 1.0, 1.0, 1.0])

    def test_matrix_is_symmetric(self):
        R = np.random.default_rng(0).standard_normal((100, 3))
        rho = correlation_matrix(R)
        np.testing.assert_allclose(rho, rho.T)

    def test_one_dim_input_raises(self):
        with pytest.raises(ValueError, match="2-D"):
            correlation_matrix(np.arange(10))

    def test_too_few_observations_raises(self):
        with pytest.raises(ValueError, match="at least 2"):
            correlation_matrix(np.array([[1.0, 2.0]]))

    def test_constant_column_yields_zero_off_diagonal(self):
        """A constant series has zero variance — correlation undefined; return 0."""
        R = np.column_stack([
            np.ones(50),  # constant
            np.arange(50, dtype=np.float64),
        ])
        rho = correlation_matrix(R)
        assert rho[0, 1] == 0.0
