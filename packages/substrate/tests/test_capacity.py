"""Tests for substrate.capacity — strategy capacity analysis.

Pins:
  1. Analytical sqrt-law solution matches bisection (closed-form check)
  2. capacity_adv is exactly ADV × pct
  3. capacity_l2 monotone: smaller tolerance → smaller max size
  4. analyze_capacity picks the correct binding constraint under each regime
  5. Capacity curve is monotone non-increasing in net alpha
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from rift_substrate.capacity import (
    CapacityResult,
    analyze_capacity,
    capacity_adv,
    capacity_impact,
    capacity_l2_depth,
)
from rift_substrate.frictions.impact import EmpiricalImpactFitter, SqrtLawImpact
from rift_substrate.frictions.slippage import L2Level


# ─── capacity_impact ─────────────────────────────────────────────────


class TestCapacityImpact:
    def test_matches_sqrt_law_analytical(self):
        """For sqrt-law, capacity at half-alpha is analytical: ADV × (alpha/(γ·σ·10000·frac))²/frac²

        Setting impact = max_frac × alpha:
          γ · σ · √(size/ADV) · 10000 = max_frac × alpha
          √(size/ADV) = max_frac × alpha / (γ · σ · 10000)
          size = ADV × [max_frac × alpha / (γ · σ · 10000)]²
        """
        alpha_bps = 20.0
        adv_usd = 10_000_000.0
        daily_vol = 0.03
        gamma = 0.7
        max_frac = 0.5

        analytical = adv_usd * (max_frac * alpha_bps / (gamma * daily_vol * 10_000)) ** 2

        result = capacity_impact(
            alpha_bps=alpha_bps,
            impact_model=SqrtLawImpact(gamma=gamma),
            adv_usd=adv_usd,
            daily_vol=daily_vol,
            max_impact_fraction=max_frac,
        )
        assert result == pytest.approx(analytical, rel=1e-4)

    def test_breakeven_size(self):
        """max_impact_fraction=1.0 gives the size where impact == alpha exactly."""
        alpha_bps = 20.0
        result = capacity_impact(
            alpha_bps=alpha_bps,
            impact_model=SqrtLawImpact(gamma=0.7),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            max_impact_fraction=1.0,
        )
        # Verify impact at result == alpha (within tolerance)
        impact_at_result = SqrtLawImpact(gamma=0.7).predict_bps(result, 10_000_000.0, 0.03)
        assert impact_at_result == pytest.approx(alpha_bps, rel=1e-3)

    def test_zero_alpha_gives_zero_capacity(self):
        """If alpha is 0, any impact is too much → capacity = 0."""
        result = capacity_impact(
            alpha_bps=0.0,
            impact_model=SqrtLawImpact(),
            adv_usd=1_000_000.0,
            daily_vol=0.03,
        )
        assert result == 0.0

    def test_negative_alpha_gives_zero(self):
        result = capacity_impact(
            alpha_bps=-5.0,
            impact_model=SqrtLawImpact(),
            adv_usd=1_000_000.0,
            daily_vol=0.03,
        )
        assert result == 0.0

    def test_invalid_adv_gives_zero(self):
        result = capacity_impact(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=0.0,
            daily_vol=0.03,
        )
        assert result == 0.0

    def test_works_with_empirical_fitter(self):
        """Bisection should work with any ImpactModel, not just sqrt-law."""
        # Fit a power law from synthetic data
        rng = np.random.default_rng(42)
        participations = rng.uniform(1e-5, 1e-2, size=100)
        # True law: I = 50 · v^0.4
        slippages = 50.0 * participations ** 0.4 * (1 + rng.normal(0, 0.05, size=100))

        fitter = EmpiricalImpactFitter().fit(participations, slippages)
        assert fitter.a is not None

        result = capacity_impact(
            alpha_bps=10.0,
            impact_model=fitter,
            adv_usd=10_000_000.0,
            daily_vol=0.03,  # ignored by empirical fitter
            max_impact_fraction=0.5,
        )
        assert result > 0
        # Verify at result, impact ≈ 5 bps (half of 10)
        impact_at = fitter.predict_bps(result, 10_000_000.0, 0.03)
        assert impact_at == pytest.approx(5.0, rel=0.05)

    def test_huge_alpha_relative_to_impact_returns_upper_bound(self):
        """If alpha is so large that even huge sizes don't bind, return upper."""
        result = capacity_impact(
            alpha_bps=1_000_000.0,  # absurd alpha
            impact_model=SqrtLawImpact(gamma=0.01),  # tiny impact
            adv_usd=1_000_000.0,
            daily_vol=0.001,
            search_upper_usd=1e8,
        )
        # Should return the upper bound (unconstrained)
        assert result == pytest.approx(1e8)


# ─── capacity_adv ────────────────────────────────────────────────────


class TestCapacityADV:
    def test_simple_multiplication(self):
        assert capacity_adv(1_000_000.0, 0.05) == pytest.approx(50_000.0)

    def test_default_pct(self):
        # Default is 5%
        assert capacity_adv(1_000_000.0) == pytest.approx(50_000.0)

    def test_zero_adv(self):
        assert capacity_adv(0.0, 0.05) == 0.0

    def test_negative_adv(self):
        assert capacity_adv(-100.0, 0.05) == 0.0

    def test_nan_adv(self):
        assert capacity_adv(float("nan"), 0.05) == 0.0

    def test_zero_pct(self):
        assert capacity_adv(1_000_000.0, 0.0) == 0.0


# ─── capacity_l2_depth ───────────────────────────────────────────────


class TestCapacityL2Depth:
    def _book(self, levels: list[tuple[float, float]]) -> list[L2Level]:
        return [L2Level(p, s) for p, s in levels]

    def test_single_level_buy(self):
        """One ask level at $100.5 with size 100. Mid is $100.
        Slippage at any size = 50 bps (fills entirely at $100.5).
        At 50 bps tol, all 100 size fills → $10,050.
        At 49 bps tol, nothing fills → $0.
        """
        book = self._book([(100.5, 100)])
        result_at_50 = capacity_l2_depth(book, 100.0, "buy", max_slippage_bps=50.0)
        # The full book is fillable at 50 bps slip
        # max possible notional = 100 * 100.5 = 10050
        assert result_at_50 == pytest.approx(10050.0, abs=1.0)

        result_at_49 = capacity_l2_depth(book, 100.0, "buy", max_slippage_bps=49.9)
        # Can't fill even the smallest size within 49.9 bps (all fills at $100.5 = 50 bps)
        assert result_at_49 < 1.0

    def test_walks_multiple_levels(self):
        """Buy walks asks: 100 @ 100.1 (10 bps), 100 @ 100.5 (50 bps).
        VWAP for 150 size: (100×100.1 + 50×100.5) / 150 = $100.233 (23.3 bps).
        For 100 size: $100.1 (10 bps).
        At max_slippage=15 bps, max size somewhere between 100 and 150.
        """
        book = self._book([(100.1, 100), (100.5, 100)])
        result = capacity_l2_depth(book, 100.0, "buy", max_slippage_bps=15.0)
        # Should fall between $10,010 (size 100) and $15,000ish
        assert 10_000 < result < 15_000

    def test_monotone_in_tolerance(self):
        """Wider tolerance → larger capacity."""
        book = self._book([(100.1, 100), (100.5, 100), (101.0, 200)])
        cap_low = capacity_l2_depth(book, 100.0, "buy", max_slippage_bps=10.0)
        cap_mid = capacity_l2_depth(book, 100.0, "buy", max_slippage_bps=30.0)
        cap_hi = capacity_l2_depth(book, 100.0, "buy", max_slippage_bps=100.0)
        assert cap_low <= cap_mid <= cap_hi

    def test_empty_book(self):
        assert capacity_l2_depth([], 100.0, "buy", 10.0) == 0.0

    def test_invalid_mid(self):
        book = self._book([(100.5, 100)])
        assert capacity_l2_depth(book, 0.0, "buy", 10.0) == 0.0
        assert capacity_l2_depth(book, float("nan"), "buy", 10.0) == 0.0

    def test_zero_tolerance(self):
        book = self._book([(100.5, 100)])
        assert capacity_l2_depth(book, 100.0, "buy", 0.0) == 0.0

    def test_sell_side_walks_bids(self):
        """Sell walks bids. With bids [99.9 @ 100, 99.5 @ 100], slippage from mid 100.
        Selling 100 @ 99.9 → 10 bps slip. Selling 150 → VWAP = (100×99.9+50×99.5)/150 ≈ 23 bps.
        """
        book = self._book([(99.9, 100), (99.5, 100)])  # bids descending
        result = capacity_l2_depth(book, 100.0, "sell", max_slippage_bps=15.0)
        assert 9_900 < result < 15_000  # between size 100 and 150


# ─── analyze_capacity ────────────────────────────────────────────────


class TestAnalyzeCapacity:
    def test_returns_full_result(self):
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
        )
        assert isinstance(r, CapacityResult)
        assert r.alpha_bps == 20.0
        assert r.adv_usd == 10_000_000.0
        assert r.binding_constraint in ("impact", "adv", "l2_depth")

    def test_impact_binds_when_alpha_thin(self):
        """Thin alpha + large ADV → impact binds before ADV."""
        r = analyze_capacity(
            alpha_bps=5.0,         # thin alpha
            impact_model=SqrtLawImpact(gamma=0.7),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            max_adv_pct=0.05,     # ADV constraint = $500K
        )
        assert r.binding_constraint == "impact"
        assert r.max_trade_size_usd < r.adv_constraint_usd

    def test_adv_binds_when_alpha_huge(self):
        """Huge alpha → impact constraint is large → ADV binds first."""
        r = analyze_capacity(
            alpha_bps=500.0,       # huge alpha
            impact_model=SqrtLawImpact(gamma=0.7),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            max_adv_pct=0.05,
        )
        assert r.binding_constraint == "adv"
        assert r.max_trade_size_usd == pytest.approx(500_000.0)

    def test_l2_binds_when_book_thin(self):
        """Thin book → L2 binds before impact or ADV."""
        book = [L2Level(100.5, 5)]  # only $502.50 of notional available
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            book_side=book,
            mid_price=100.0,
            side="buy",
            max_slippage_bps=50.0,
        )
        assert r.binding_constraint == "l2_depth"
        assert r.max_trade_size_usd < r.impact_constraint_usd
        assert r.max_trade_size_usd < r.adv_constraint_usd

    def test_l2_nan_when_book_not_provided(self):
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
        )
        assert math.isnan(r.l2_constraint_usd)
        # Binding constraint must NOT be l2_depth when book not provided
        assert r.binding_constraint != "l2_depth"

    def test_capacity_curve_monotone_in_size(self):
        """Net alpha must be monotonically non-increasing in trade size."""
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            n_curve_points=20,
        )
        nets = [p.net_alpha_bps for p in r.capacity_curve]
        for a, b in zip(nets, nets[1:]):
            assert b <= a + 1e-9, "net alpha must be non-increasing in size"

    def test_capacity_curve_has_positive_then_negative_net_alpha(self):
        """Curve should cross from positive net alpha to negative as size grows."""
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            n_curve_points=30,
        )
        nets = [p.net_alpha_bps for p in r.capacity_curve]
        # First point: positive (impact small) — but curve starts at curve_upper/N,
        # not at zero, so we just check it's a meaningful fraction of alpha
        assert nets[0] > 0
        # Last point: net alpha at 2× the impact-binding constraint must be deep
        # negative (impact has overwhelmed alpha)
        assert nets[-1] < 0
        # Crossing point: somewhere in the curve
        assert any(n > 0 for n in nets) and any(n < 0 for n in nets)

    def test_half_alpha_equals_impact_constraint(self):
        """half_alpha_size_usd is the impact-constraint with max_frac=0.5."""
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
            max_impact_fraction=0.5,
        )
        assert r.half_alpha_size_usd == pytest.approx(r.impact_constraint_usd, rel=1e-6)

    def test_breakeven_larger_than_half_alpha(self):
        """Breakeven (impact==alpha) must be larger than half-alpha (impact==alpha/2)."""
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
        )
        assert r.breakeven_size_usd > r.half_alpha_size_usd

    def test_summary_renders(self):
        r = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
        )
        s = r.summary()
        assert "CapacityResult" in s
        assert "Max trade size" in s
        assert "Impact" in s
        assert "ADV-based" in s
