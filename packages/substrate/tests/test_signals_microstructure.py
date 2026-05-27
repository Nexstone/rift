"""Tests for substrate.signals.microstructure — OFI / wall / spread primitives.

Pins:
  - book_imbalance:        (bid - ask) / (bid + ask), NaN on empty book
  - book_imbalance_zscore: rolling stat; first N-1 rows NaN
  - wall_intensity:        wall / depth, NaN on zero depth
  - spread_pressure:       normalized; rejects non-positive typical
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.signals import (
    book_imbalance,
    book_imbalance_zscore,
    spread_pressure,
    wall_intensity,
)


# ─── book_imbalance ─────────────────────────────────────────────────


class TestBookImbalance:
    def test_balanced_book_is_zero(self):
        result = book_imbalance([100.0], [100.0])
        assert result[0] == pytest.approx(0.0)

    def test_bid_dominant_is_positive(self):
        # bid 200, ask 100 → (200-100)/(200+100) = 0.333
        result = book_imbalance([200.0], [100.0])
        assert result[0] == pytest.approx(1.0 / 3.0)

    def test_ask_dominant_is_negative(self):
        result = book_imbalance([100.0], [200.0])
        assert result[0] == pytest.approx(-1.0 / 3.0)

    def test_empty_book_is_nan(self):
        result = book_imbalance([0.0], [0.0])
        assert np.isnan(result[0])

    def test_bounded_in_neg1_pos1(self):
        result = book_imbalance([1e9, 0.0, 100.0, 50.0], [0.0, 1e9, 0.0, 100.0])
        finite = result[np.isfinite(result)]
        assert (finite >= -1.0).all() and (finite <= 1.0).all()

    def test_shape_mismatch_raises(self):
        with pytest.raises(ValueError, match="shape mismatch"):
            book_imbalance([1.0, 2.0], [1.0])


# ─── book_imbalance_zscore ──────────────────────────────────────────


class TestBookImbalanceZscore:
    def test_first_window_minus_one_rows_nan(self):
        s = np.linspace(-0.5, 0.5, 50)
        z = book_imbalance_zscore(s, window=24)
        assert np.isnan(z[:23]).all()
        assert np.isfinite(z[23])

    def test_constant_input_is_nan_zscore(self):
        """Constant series → zero std → z undefined → NaN."""
        z = book_imbalance_zscore([0.1] * 30, window=10)
        assert np.isnan(z[10:]).all()

    def test_normal_input_centered(self):
        """Normally distributed input → z-scores near zero on average."""
        rng = np.random.default_rng(42)
        s = rng.normal(0, 0.1, size=200)
        z = book_imbalance_zscore(s, window=24)
        z_clean = z[np.isfinite(z)]
        assert abs(z_clean.mean()) < 0.2  # mean near 0
        assert 0.5 < z_clean.std() < 1.5  # std around 1

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError, match="window"):
            book_imbalance_zscore([1.0, 2.0], window=1)


# ─── wall_intensity ─────────────────────────────────────────────────


class TestWallIntensity:
    def test_single_level_is_one(self):
        """Wall == total depth → intensity = 1.0 (one level concentrates everything)."""
        result = wall_intensity([1000.0], [1000.0])
        assert result[0] == pytest.approx(1.0)

    def test_evenly_distributed_is_low(self):
        """Wall = 100, depth = 1000 → intensity = 0.1 (one of 10 equal levels)."""
        result = wall_intensity([100.0], [1000.0])
        assert result[0] == pytest.approx(0.1)

    def test_zero_depth_is_nan(self):
        result = wall_intensity([100.0], [0.0])
        assert np.isnan(result[0])

    def test_bounded_in_0_1(self):
        result = wall_intensity([100.0, 500.0, 999.0], [1000.0, 1000.0, 1000.0])
        finite = result[np.isfinite(result)]
        assert (finite >= 0.0).all() and (finite <= 1.0).all()


# ─── spread_pressure ────────────────────────────────────────────────


class TestSpreadPressure:
    def test_typical_spread_returns_one(self):
        result = spread_pressure([5.0], typical_spread_bps=5.0)
        assert result[0] == pytest.approx(1.0)

    def test_wide_spread_above_one(self):
        result = spread_pressure([15.0], typical_spread_bps=5.0)
        assert result[0] == pytest.approx(3.0)

    def test_tight_spread_below_one(self):
        result = spread_pressure([1.0], typical_spread_bps=5.0)
        assert result[0] == pytest.approx(0.2)

    def test_invalid_typical_raises(self):
        with pytest.raises(ValueError, match="typical_spread_bps"):
            spread_pressure([1.0], typical_spread_bps=0.0)


# ─── Integration: end-to-end with decay.compute_ic_curve ────────────


class TestComposesWithDecayAnalysis:
    """The OFI primitive should drop into the existing IC-vs-horizon pipeline."""

    def test_imbalance_signal_runs_through_decay_pipeline(self):
        from rift_substrate.decay import compute_ic_curve, make_forward_returns

        rng = np.random.default_rng(0)
        T = 500
        bid = rng.uniform(50, 200, size=T)
        ask = rng.uniform(50, 200, size=T)
        # Synthetic prices: random walk that slightly correlates with imbalance
        imb = book_imbalance(bid, ask)
        prices = 100.0 + np.cumsum(0.01 * np.nan_to_num(imb) + 0.1 * rng.standard_normal(T))

        fwd = make_forward_returns(prices, [1, 5, 10])
        curve = compute_ic_curve(
            signal=imb, forward_returns=fwd, horizons=[1, 5, 10],
            method="spearman", n_bootstrap=0,
        )
        # Just verify the pipeline runs cleanly + returns valid ICs
        assert curve.horizons.size == 3
        assert np.isfinite(curve.ics).all()
