"""Tests for rift_substrate.regime.changepoints — PELT structural breaks."""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.regime import (
    ChangepointResult,
    detect_changepoints,
    regime_segments,
)


class TestDetectChangepoints:
    def test_finds_change_in_synthetic_two_regime_series(self):
        rng = np.random.default_rng(42)
        # Two regimes glued together: low-vol then high-vol
        regime1 = rng.normal(0, 0.5, 200)
        regime2 = rng.normal(0, 2.5, 200)
        series = np.concatenate([regime1, regime2])

        result = detect_changepoints(series, model="rbf", penalty=10.0)
        assert isinstance(result, ChangepointResult)
        assert result.n_breakpoints >= 1
        # The known break is at index 200; at least one detected break
        # should fall within 30 bars of it.
        assert any(abs(bp - 200) < 30 for bp in result.breakpoints), (
            f"expected a break near 200, got {result.breakpoints}"
        )

    def test_no_break_in_stationary_series(self):
        rng = np.random.default_rng(43)
        series = rng.normal(0, 1, 500)
        # High penalty + stationary data → no breaks
        result = detect_changepoints(series, model="rbf", penalty=200.0)
        assert result.n_breakpoints == 0

    def test_too_short_series_returns_empty(self):
        series = np.array([1.0, 2.0, 3.0])
        result = detect_changepoints(series, min_size=20)
        assert result.n_breakpoints == 0
        assert result.n_obs == 3

    def test_rejects_non_1d(self):
        with pytest.raises(ValueError, match="1D"):
            detect_changepoints(np.zeros((10, 2)))

    def test_accepts_python_list(self):
        rng = np.random.default_rng(44)
        series = list(rng.normal(0, 1, 300))
        result = detect_changepoints(series, penalty=20.0)
        assert isinstance(result, ChangepointResult)

    def test_supports_different_cost_models(self):
        rng = np.random.default_rng(45)
        # Mean-shift series
        series = np.concatenate([rng.normal(0, 0.5, 100), rng.normal(5, 0.5, 100)])
        for model in ("rbf", "l2", "normal", "l1"):
            result = detect_changepoints(series, model=model, penalty=5.0, min_size=10)
            assert result.model == model
            # All cost models should detect this clear mean shift
            assert result.n_breakpoints >= 1


class TestRegimeSegments:
    def test_segments_partition_the_series(self):
        rng = np.random.default_rng(46)
        series = np.concatenate([
            rng.normal(0, 0.5, 100),
            rng.normal(0, 2.0, 100),
            rng.normal(0, 0.5, 100),
        ])
        segs = regime_segments(series, penalty=5.0)
        # Reconstruct: start at 0, end at len(series), no gaps
        assert segs[0][0] == 0
        assert segs[-1][1] == len(series)
        for i in range(len(segs) - 1):
            assert segs[i][1] == segs[i + 1][0]

    def test_single_segment_when_stationary(self):
        rng = np.random.default_rng(47)
        series = rng.normal(0, 1, 300)
        segs = regime_segments(series, penalty=500.0)
        assert len(segs) == 1
        assert segs[0] == (0, 300)

    def test_segment_indices_are_valid(self):
        rng = np.random.default_rng(48)
        series = np.concatenate([rng.normal(0, 0.5, 150), rng.normal(0, 2.0, 150)])
        segs = regime_segments(series, penalty=10.0)
        # Every segment is non-empty and within bounds
        for start, end in segs:
            assert 0 <= start < end <= len(series)
