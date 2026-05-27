"""Tests for substrate.frictions — fees, funding, markouts, shortfall, slippage, impact, cost.

Each TestClass pins the math + sign-convention invariants for one sub-module.
The aggregator (`TestTradeCost`) exercises composition end-to-end.
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.frictions import (
    DEFAULT_HORIZONS_SECONDS,
    EmpiricalImpactFitter,
    Fill,
    FundingAccrual,
    ImplementationShortfall,
    L2Level,
    MarkoutSeries,
    SqrtLawImpact,
    TradeCost,
    accrue_funding,
    compute_markouts,
    estimate_fee,
    estimate_trade_cost,
    expected_funding_cost,
    implementation_shortfall,
    load_default_schedule,
    sqrt_law_impact_bps,
    walk_book,
)


# ═══════════════════════════════════════════════════════════════════════
# fees
# ═══════════════════════════════════════════════════════════════════════


class TestFees:
    def test_tier_0_perp_taker_includes_builder_fee_by_default(self):
        q = estimate_fee(10_000, is_taker=True, instrument="perp")
        # Calibrations: tier 0 = 4.5 bps taker base + 3.0 bps builder = 7.5 bps total
        assert q.base_bps == pytest.approx(4.5)
        assert q.builder_bps == pytest.approx(3.0)
        assert q.total_bps == pytest.approx(7.5)
        assert q.total_usd == pytest.approx(7.5)

    def test_tier_0_perp_maker(self):
        q = estimate_fee(10_000, is_taker=False, instrument="perp")
        assert q.base_bps == pytest.approx(1.5)
        assert q.total_bps == pytest.approx(4.5)

    def test_high_tier_maker_rebate_carried_through(self):
        """Tier 5 perp maker has NEGATIVE base fee (rebate). Builder fee still positive."""
        q = estimate_fee(10_000, is_taker=False, instrument="perp",
                         tier_volume_14d_usd=3_000_000_000)
        assert q.base_bps == pytest.approx(-0.1)
        assert q.total_bps == pytest.approx(2.9)  # -0.1 + 3.0 builder

    def test_spot_builder_fee_is_100bps(self):
        q = estimate_fee(10_000, is_taker=True, instrument="spot")
        assert q.builder_bps == pytest.approx(100.0)
        # Total = 7 bps base taker + 100 bps builder
        assert q.total_bps == pytest.approx(107.0)

    def test_builder_off(self):
        q = estimate_fee(10_000, is_taker=True, instrument="perp",
                         include_builder_fee=False)
        assert q.builder_bps == 0.0
        assert q.total_bps == pytest.approx(4.5)

    def test_invalid_instrument_raises(self):
        with pytest.raises(ValueError, match="instrument"):
            estimate_fee(10_000, is_taker=True, instrument="invalid")

    def test_negative_notional_raises(self):
        with pytest.raises(ValueError, match="notional"):
            estimate_fee(-100, is_taker=True)

    def test_schedule_loads(self):
        s = load_default_schedule()
        assert len(s.perp_tiers) > 0
        assert s.builder_fee_bps_perp == pytest.approx(3.0)
        assert s.builder_address.startswith("0x")


# ═══════════════════════════════════════════════════════════════════════
# funding
# ═══════════════════════════════════════════════════════════════════════


class TestFunding:
    def test_long_pays_positive_funding(self):
        """Long position pays when funding rate > 0."""
        rates = [0.0001] * 24  # 24h at 0.01%/h
        r = accrue_funding("long", notional_usd=100_000, funding_rates=rates)
        # Cumulative = 24 * 0.0001 = 0.0024 → $100k * 0.0024 = $240
        assert r.total_paid_usd == pytest.approx(240.0)
        assert r.intervals_held == 24

    def test_short_receives_positive_funding(self):
        """Short side: same rates → negative cost (income)."""
        rates = [0.0001] * 24
        r = accrue_funding("short", notional_usd=100_000, funding_rates=rates)
        assert r.total_paid_usd == pytest.approx(-240.0)

    def test_zero_rate_series_returns_zero(self):
        r = accrue_funding("long", 100_000, [])
        assert r.total_paid_usd == 0.0
        assert r.intervals_held == 0

    def test_nan_rates_dropped(self):
        rates = [0.0001, np.nan, 0.0001, 0.0001]
        r = accrue_funding("long", 100_000, rates)
        # 3 valid rates of 0.0001 each
        assert r.intervals_held == 3
        assert r.total_paid_usd == pytest.approx(30.0)  # 3 * 0.0001 * 100_000

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="position_side"):
            accrue_funding("buy", 100_000, [0.0001])  # buy/sell not accepted here

    def test_expected_cost_flat_rate(self):
        """Flat rate × intervals × notional."""
        cost = expected_funding_cost("long", 100_000, current_rate=0.0001,
                                     holding_period_hours=8)
        # 8 intervals at 0.0001 → $80
        assert cost == pytest.approx(80.0)

    def test_expected_cost_with_drift(self):
        """Mean-reverting drift reduces cumulative."""
        cost_flat = expected_funding_cost("long", 100_000, 0.0001, 8)
        cost_drifting = expected_funding_cost(
            "long", 100_000, 0.0001, 8, rate_drift_per_hour=-0.00001
        )
        assert cost_drifting < cost_flat

    def test_expected_cost_zero_holding(self):
        assert expected_funding_cost("long", 100_000, 0.0001, 0) == 0.0


# ═══════════════════════════════════════════════════════════════════════
# markouts
# ═══════════════════════════════════════════════════════════════════════


class TestMarkouts:
    def test_long_favorable_move_is_positive_markout(self):
        # Filled long at $100, price rises to $101 at t=10s
        ts = np.array([1_000, 5_000, 30_000])
        px = np.array([100.5, 101.0, 102.0])
        r = compute_markouts(100.0, 0, "long", ts, px, horizons_seconds=[1, 10, 30])
        # t+1s: $100.50 → +50 bps
        # t+10s: $101 (forward-fill from 5s) → +100 bps
        # t+30s: $102 → +200 bps
        assert r.markouts_bps[0] == pytest.approx(50.0)
        assert r.markouts_bps[1] == pytest.approx(100.0)
        assert r.markouts_bps[2] == pytest.approx(200.0)

    def test_short_same_data_is_sign_flipped(self):
        ts = np.array([1_000, 5_000, 30_000])
        px = np.array([100.5, 101.0, 102.0])
        r = compute_markouts(100.0, 0, "short", ts, px, horizons_seconds=[1, 10, 30])
        assert r.markouts_bps[0] == pytest.approx(-50.0)

    def test_horizon_past_data_is_nan(self):
        ts = np.array([1_000])  # only 1s of data
        px = np.array([100.5])
        r = compute_markouts(100.0, 0, "long", ts, px, horizons_seconds=[60])
        assert np.isnan(r.markouts_bps[0])

    def test_empty_series_all_nan(self):
        r = compute_markouts(100.0, 0, "long",
                             np.array([]), np.array([]),
                             horizons_seconds=[1, 10])
        assert all(np.isnan(m) for m in r.markouts_bps)

    def test_default_horizons(self):
        ts = np.array([1_000, 10_000, 60_000, 300_000])
        px = np.array([100.5, 101.0, 102.0, 103.0])
        r = compute_markouts(100.0, 0, "long", ts, px)
        assert r.horizons_seconds == DEFAULT_HORIZONS_SECONDS

    def test_at_lookup(self):
        ts = np.array([1_000, 10_000])
        px = np.array([100.5, 101.0])
        r = compute_markouts(100.0, 0, "long", ts, px, horizons_seconds=[1, 10])
        assert r.at(1) == pytest.approx(50.0)
        assert np.isnan(r.at(999))  # not in horizons


# ═══════════════════════════════════════════════════════════════════════
# Implementation Shortfall
# ═══════════════════════════════════════════════════════════════════════


class TestImplementationShortfall:
    def test_buy_full_fill_timing_cost_only(self):
        """Buy 100 @ decision $100, fills at $100.50, final mid $101."""
        fills = [Fill(timestamp_ms=1000, price=100.50, size=100.0, fee_usd=5.0)]
        r = implementation_shortfall("buy", 100.0, 100.0, fills, 101.0)
        # Timing: 100 * (100.50 - 100) = $50
        # Opportunity: 0 (no unfilled)
        # Commission: $5
        # Total: $55
        assert r.timing_cost_usd == pytest.approx(50.0)
        assert r.opportunity_cost_usd == pytest.approx(0.0)
        assert r.commission_usd == pytest.approx(5.0)
        assert r.total_shortfall_usd == pytest.approx(55.0)
        assert r.total_shortfall_bps == pytest.approx(55.0)  # $55 / $10k * 10000

    def test_buy_partial_fill_opportunity_cost(self):
        """Same buy but only 50% filled."""
        fills = [Fill(timestamp_ms=1000, price=100.50, size=50.0, fee_usd=2.5)]
        r = implementation_shortfall("buy", 100.0, 100.0, fills, 101.0)
        # Timing on 50: 50 * 0.50 = $25
        # Opportunity on 50: 50 * (101 - 100) = $50
        # Commission: $2.50
        assert r.timing_cost_usd == pytest.approx(25.0)
        assert r.opportunity_cost_usd == pytest.approx(50.0)
        assert r.total_shortfall_usd == pytest.approx(77.5)

    def test_sell_signs_flipped(self):
        """Sell 100 @ decision $100, fills at $99.50 (below decision = cost)."""
        fills = [Fill(timestamp_ms=1000, price=99.50, size=100.0, fee_usd=5.0)]
        r = implementation_shortfall("sell", 100.0, 100.0, fills, 99.0)
        # Timing: -1 * 100 * (99.50 - 100) = +50 (cost)
        # Opportunity: 0
        # Commission: +5
        assert r.timing_cost_usd == pytest.approx(50.0)
        assert r.total_shortfall_usd == pytest.approx(55.0)

    def test_no_fills_only_opportunity(self):
        """Cancelled order — no fills, all opportunity cost."""
        r = implementation_shortfall("buy", 100.0, 100.0, [], 101.0)
        assert r.timing_cost_usd == 0.0
        assert r.opportunity_cost_usd == pytest.approx(100.0)  # 100 * (101-100)
        assert r.commission_usd == 0.0
        assert r.filled_size == 0.0
        assert r.unfilled_size == 100.0

    def test_overfill_raises(self):
        fills = [Fill(timestamp_ms=1000, price=100.0, size=200.0, fee_usd=0)]
        with pytest.raises(ValueError, match="over-fill"):
            implementation_shortfall("buy", 100.0, 100.0, fills, 100.0)


# ═══════════════════════════════════════════════════════════════════════
# L2 walk slippage
# ═══════════════════════════════════════════════════════════════════════


class TestSlippage:
    def test_buy_walks_asks_correctly(self):
        # Mid $100, asks $100.05 (3), $100.10 (5)
        asks = [L2Level(100.05, 3.0), L2Level(100.10, 5.0)]
        r = walk_book("buy", 5.0, asks, mid_price=100.0)
        # VWAP = (3*100.05 + 2*100.10) / 5 = 100.07
        # Slippage (buy) = (100.07 - 100) / 100 * 10000 = 7 bps
        assert r.fill_vwap == pytest.approx(100.07)
        assert r.slippage_bps == pytest.approx(7.0)
        assert r.filled_size == 5.0
        assert r.unfilled_size == 0.0
        assert r.n_levels_consumed == 2

    def test_sell_walks_bids_correctly(self):
        # Mid $100, bids $99.95 (2), $99.90 (3), $99.80 (1)
        bids = [L2Level(99.95, 2.0), L2Level(99.90, 3.0), L2Level(99.80, 1.0)]
        r = walk_book("sell", 8.0, bids, mid_price=100.0)
        # VWAP on 6 filled = (2*99.95 + 3*99.90 + 1*99.80) / 6 = 99.90
        # Slippage (sell) = -1 * (99.90 - 100) / 100 * 10000 = +10 bps (cost)
        assert r.fill_vwap == pytest.approx(99.90, abs=1e-9)
        assert r.slippage_bps == pytest.approx(10.0)
        assert r.filled_size == 6.0
        assert r.unfilled_size == 2.0

    def test_empty_book_no_fill(self):
        r = walk_book("buy", 1.0, [], mid_price=100.0)
        assert r.filled_size == 0.0
        assert np.isnan(r.fill_vwap)
        assert np.isnan(r.slippage_bps)

    def test_zero_size_request_no_fill(self):
        asks = [L2Level(100.05, 10.0)]
        r = walk_book("buy", 0.0, asks, mid_price=100.0)
        assert r.filled_size == 0.0

    def test_negative_price_levels_skipped(self):
        asks = [L2Level(-1.0, 100.0), L2Level(100.05, 3.0)]
        r = walk_book("buy", 3.0, asks, mid_price=100.0)
        assert r.fill_vwap == pytest.approx(100.05)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            walk_book("foo", 1.0, [L2Level(100, 1)], 100.0)


# ═══════════════════════════════════════════════════════════════════════
# Impact models
# ═══════════════════════════════════════════════════════════════════════


class TestImpact:
    def test_sqrt_law_known_value(self):
        # γ=0.7, daily_vol=3%, participation=0.01% → 0.7 * 0.03 * 0.01 * 10000 = 2.1
        bps = sqrt_law_impact_bps(
            trade_size_usd=100_000, adv_usd=1_000_000_000,
            daily_vol=0.03, gamma=0.7,
        )
        assert bps == pytest.approx(2.1)

    def test_sqrt_law_zero_size(self):
        assert sqrt_law_impact_bps(0, 1e9, 0.02) == 0.0

    def test_sqrt_law_invalid_adv(self):
        assert np.isnan(sqrt_law_impact_bps(100_000, 0, 0.02))

    def test_SqrtLawImpact_class_matches_function(self):
        m = SqrtLawImpact(gamma=0.7)
        assert m.predict_bps(100_000, 1e9, 0.03) == pytest.approx(2.1)

    def test_empirical_fitter_recovers_sqrt_law_synthetic(self):
        rng = np.random.default_rng(0)
        true_a, true_b = 5.0, 0.5
        n = 200
        p = rng.uniform(0.0001, 0.01, n)
        s = true_a * p ** true_b * rng.lognormal(0, 0.2, n)
        fitter = EmpiricalImpactFitter().fit(p, s)
        # Tight recovery on this much data
        assert abs(fitter.b - true_b) < 0.05
        assert fitter.r_squared > 0.7
        assert fitter.n_samples == n

    def test_empirical_fitter_handles_non_sqrt_data(self):
        """Crypto-style flat impact (b=0.3) — fitter recovers it."""
        rng = np.random.default_rng(7)
        p = rng.uniform(0.0001, 0.01, 200)
        s = 10.0 * p ** 0.3 * rng.lognormal(0, 0.15, 200)
        fitter = EmpiricalImpactFitter().fit(p, s)
        assert abs(fitter.b - 0.3) < 0.05

    def test_empirical_fitter_refuses_too_little_data(self):
        fitter = EmpiricalImpactFitter().fit([0.001, 0.002], [1.0, 2.0])
        assert fitter.a is None
        assert fitter.b is None

    def test_unfitted_returns_nan(self):
        f = EmpiricalImpactFitter()
        assert np.isnan(f.predict_bps(1e6, 1e9, 0.03))

    def test_empirical_fitter_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length"):
            EmpiricalImpactFitter().fit([0.001, 0.002], [1.0])


# ═══════════════════════════════════════════════════════════════════════
# TradeCost aggregator
# ═══════════════════════════════════════════════════════════════════════


class TestTradeCost:
    def test_entry_only_no_funding(self):
        cost = estimate_trade_cost(
            side="buy", notional_usd=10_000, mid_price=70_000,
            adv_usd=2.5e9, daily_vol=0.03,
        )
        # Fees: 7.5 bps; impact: ~0.42 bps; funding: 0; slippage: 0
        assert cost.fee_bps == pytest.approx(7.5)
        assert cost.funding_bps == 0.0
        assert cost.slippage_bps == 0.0
        assert cost.impact_bps > 0  # sqrt-law positive
        assert cost.total_bps == pytest.approx(
            cost.fee_bps + cost.impact_bps + cost.funding_bps + cost.slippage_bps
        )

    def test_funding_added_when_holding(self):
        cost = estimate_trade_cost(
            side="buy", notional_usd=10_000, mid_price=70_000,
            adv_usd=2.5e9, daily_vol=0.03,
            holding_period_hours=8, current_funding_rate=0.0001,
        )
        # Long pays 8h * 0.01%/h = 0.08% = 8 bps
        assert cost.funding_bps == pytest.approx(8.0)
        assert cost.funding_usd == pytest.approx(8.0)

    def test_short_funding_is_income_when_rate_positive(self):
        cost = estimate_trade_cost(
            side="sell", notional_usd=10_000, mid_price=70_000,
            adv_usd=2.5e9, daily_vol=0.03,
            holding_period_hours=24, current_funding_rate=0.0002,
        )
        # 24h * 0.02%/h = 0.48% = 48 bps; short → negative cost
        assert cost.funding_bps == pytest.approx(-48.0)
        assert cost.funding_usd == pytest.approx(-48.0)

    def test_slippage_added_when_book_supplied(self):
        asks = [L2Level(70_001, 0.5), L2Level(70_005, 1.0), L2Level(70_020, 2.0)]
        cost = estimate_trade_cost(
            side="buy", notional_usd=10_000, mid_price=70_000,
            adv_usd=2.5e9, daily_vol=0.03,
            book_side=asks,
        )
        assert cost.slippage_bps > 0
        assert cost.book_filled_size > 0
        assert cost.book_unfilled_size == 0

    def test_side_aliases(self):
        """'buy' and 'long' should give equivalent results (same trade)."""
        c1 = estimate_trade_cost(
            side="buy", notional_usd=10_000, mid_price=70_000,
            adv_usd=2.5e9, daily_vol=0.03,
        )
        c2 = estimate_trade_cost(
            side="long", notional_usd=10_000, mid_price=70_000,
            adv_usd=2.5e9, daily_vol=0.03,
        )
        assert c1.total_bps == c2.total_bps

    def test_no_adv_means_no_impact(self):
        cost = estimate_trade_cost(
            side="buy", notional_usd=10_000, mid_price=70_000,
            adv_usd=None, daily_vol=0.03,
        )
        assert cost.impact_bps == 0.0
        assert cost.fee_bps == pytest.approx(7.5)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            estimate_trade_cost(side="foo", notional_usd=10_000, mid_price=70_000)
