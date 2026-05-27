"""Unit tests for substrate.regime.hmm.

These tests pin the behaviour ported from the original `_HMM_HELPERS` /
`_HMM_STRATEGY_METHODS` string templates in `rift_engine.workbench`. The
math should be bit-for-bit equivalent.
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.regime import HMMRegimeDetector
from rift_substrate.regime.hmm import classify_states, compute_hmm_features


# ─── compute_hmm_features ─────────────────────────────────────────────


class TestComputeHmmFeatures:
    def test_shape_is_n_by_three(self):
        closes = [100.0] * 50
        funding = [0.0001] * 50
        features, valid_from = compute_hmm_features(closes, funding, vol_window=10)
        assert features.shape == (50, 3)
        assert valid_from == 10

    def test_log_returns_zero_for_constant_prices(self):
        features, _ = compute_hmm_features([100.0] * 30, [0.0] * 30, vol_window=10)
        # Column 0 = log returns. All zeros except possibly first which is also zero.
        assert np.allclose(features[:, 0], 0.0)

    def test_log_returns_match_manual_calc(self):
        closes = [100.0, 102.0, 101.0, 104.0]
        funding = [0.0, 0.0, 0.0, 0.0]
        features, _ = compute_hmm_features(closes, funding, vol_window=2)
        # log(102/100), log(101/102), log(104/101)
        expected = np.array([0.0, np.log(102 / 100), np.log(101 / 102), np.log(104 / 101)])
        np.testing.assert_allclose(features[:, 0], expected, atol=1e-9)

    def test_funding_pass_through(self):
        funding = [0.0001, -0.0002, 0.0003, -0.0004]
        features, _ = compute_hmm_features([100.0] * 4, funding, vol_window=2)
        np.testing.assert_allclose(features[:, 2], funding)

    def test_realized_vol_zero_before_window(self):
        features, valid_from = compute_hmm_features(
            list(np.random.RandomState(0).normal(100, 1, 50)),
            [0.0] * 50,
            vol_window=10,
        )
        # Realized vol is zero for the first `vol_window` indices.
        assert np.all(features[:valid_from, 1] == 0.0)

    def test_realized_vol_nonzero_after_window(self):
        np.random.seed(0)
        closes = list(100 + np.cumsum(np.random.normal(0, 1, 100)))
        features, valid_from = compute_hmm_features(closes, [0.0] * 100, vol_window=24)
        assert features[valid_from:, 1].max() > 0.0

    def test_handles_short_input(self):
        features, valid_from = compute_hmm_features([100.0], [0.0], vol_window=10)
        assert features.shape == (1, 3)
        assert valid_from == 10  # window size, even if larger than input


# ─── classify_states ──────────────────────────────────────────────────


class TestClassifyStates:
    def test_none_model_returns_default(self):
        labels = classify_states(None)
        assert labels == {"calm": 0, "volatile": 1, "crisis": 2}

    def test_three_state_sorts_by_vol_variance(self):
        # Mock a 3-state model with vol variances [0.5, 0.1, 0.9]
        # Sorted ascending: [1, 0, 2] → calm=1, volatile=0, crisis=2
        class MockModel:
            n_components = 3
            covars_ = np.array([
                [0.0, 0.5, 0.0],  # vol variance at index 1
                [0.0, 0.1, 0.0],
                [0.0, 0.9, 0.0],
            ])
        labels = classify_states(MockModel())
        assert labels == {"calm": 1, "volatile": 0, "crisis": 2}

    def test_two_state_collapses_volatile_and_crisis(self):
        class MockModel:
            n_components = 2
            covars_ = np.array([
                [0.0, 0.1, 0.0],
                [0.0, 0.9, 0.0],
            ])
        labels = classify_states(MockModel())
        assert labels["calm"] == 0
        assert labels["volatile"] == labels["crisis"] == 1

    def test_handles_2d_covariance(self):
        """Some hmmlearn config types produce 2-D cov matrices per state."""
        class MockModel:
            n_components = 3
            covars_ = np.array([
                [[1.0, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, 1.0]],
                [[1.0, 0.0, 0.0], [0.0, 0.1, 0.0], [0.0, 0.0, 1.0]],
                [[1.0, 0.0, 0.0], [0.0, 0.9, 0.0], [0.0, 0.0, 1.0]],
            ])
        labels = classify_states(MockModel())
        assert labels == {"calm": 1, "volatile": 0, "crisis": 2}


# ─── HMMRegimeDetector ────────────────────────────────────────────────


class TestHMMRegimeDetectorInit:
    def test_defaults(self):
        d = HMMRegimeDetector()
        assert d.n_states == 3
        assert d.n_restarts == 10
        assert d.vol_window == 24
        assert d.model is None
        assert d.trained is False
        assert d.state_labels == {"calm": 0, "volatile": 1, "crisis": 2}

    def test_custom_params(self):
        d = HMMRegimeDetector(n_states=2, n_restarts=5, vol_window=12)
        assert d.n_states == 2
        assert d.n_restarts == 5
        assert d.vol_window == 12


class TestHMMRegimeDetectorFit:
    def test_fit_returns_false_on_short_input(self):
        d = HMMRegimeDetector(n_states=3, n_restarts=2, vol_window=24)
        # Need at least 100 valid features (after vol_window warmup) — give less
        closes = [100.0 + i for i in range(50)]
        funding = [0.0] * 50
        assert d.fit(closes, funding) is False
        assert d.trained is False

    def test_fit_returns_true_on_sufficient_data(self):
        """With enough data, fit should succeed and set trained=True."""
        np.random.seed(42)
        # 300 candles, vol_window=24 → 276 valid features, well over the 100 minimum
        closes = list(100 + np.cumsum(np.random.normal(0, 0.5, 300)))
        funding = list(np.random.normal(0, 0.0001, 300))
        d = HMMRegimeDetector(n_states=3, n_restarts=3, vol_window=24)
        assert d.fit(closes, funding) is True
        assert d.trained is True
        assert d.model is not None
        # state_labels should now reflect the trained model
        assert set(d.state_labels.keys()) == {"calm", "volatile", "crisis"}


class TestHMMRegimeDetectorPredict:
    def test_predict_untrained_returns_none(self):
        d = HMMRegimeDetector()
        result = d.predict_regime([100.0] * 50, [0.0] * 50)
        assert result is None

    def test_predict_too_short_returns_none(self):
        """Even with a trained model, very short input → None."""
        np.random.seed(7)
        closes = list(100 + np.cumsum(np.random.normal(0, 0.5, 300)))
        funding = list(np.random.normal(0, 0.0001, 300))
        d = HMMRegimeDetector(n_states=3, n_restarts=2, vol_window=24)
        assert d.fit(closes, funding) is True
        # Input that won't yield 10 valid features (need len > vol_window + 10 = 34)
        assert d.predict_regime([100.0] * 20, [0.0] * 20) is None

    def test_predict_returns_valid_regime_label(self):
        np.random.seed(11)
        closes = list(100 + np.cumsum(np.random.normal(0, 0.5, 400)))
        funding = list(np.random.normal(0, 0.0001, 400))
        d = HMMRegimeDetector(n_states=3, n_restarts=3, vol_window=24)
        assert d.fit(closes[:300], funding[:300]) is True
        regime = d.predict_regime(closes, funding)
        assert regime in ("calm", "volatile", "crisis")
