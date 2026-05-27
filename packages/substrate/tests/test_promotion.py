"""Tests for substrate.promotion — strategy promotion gates.

Pins:
  1. Each gate returns PASS at and above the threshold; FAIL below.
  2. Composite verdict.overall_passed == all(gates passed).
  3. Edge cases: empty inputs, NaN, single observation.
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.capacity import analyze_capacity
from rift_substrate.frictions.impact import SqrtLawImpact
from rift_substrate.promotion import (
    GateResult,
    PromotionVerdict,
    evaluate_promotion,
    gate_capacity,
    gate_cv_pass_rate,
    gate_deflated_sharpe,
    gate_max_drawdown,
    gate_track_record,
)


# ─── gate_deflated_sharpe ────────────────────────────────────────────


class TestGateDeflatedSharpe:
    def test_high_sharpe_passes(self):
        """Sharpe of 2/period (annual ~32) easily clears DSR > 0.95 with no trials."""
        g = gate_deflated_sharpe(
            observed_sharpe=2.0,
            n_observations=500,
            n_trials=1,
            variance_of_trial_sharpes=0.0,
            min_dsr=0.95,
        )
        assert g.passed is True
        assert g.metric_value >= 0.95

    def test_low_sharpe_fails(self):
        """Zero Sharpe should fail."""
        g = gate_deflated_sharpe(
            observed_sharpe=0.0,
            n_observations=252,
            n_trials=1,
            variance_of_trial_sharpes=0.0,
            min_dsr=0.95,
        )
        assert g.passed is False

    def test_lots_of_trials_lowers_dsr(self):
        """With many trials, the same observed Sharpe gets deflated more aggressively."""
        kwargs = dict(observed_sharpe=0.15, n_observations=252)
        g_one_trial = gate_deflated_sharpe(
            **kwargs, n_trials=1, variance_of_trial_sharpes=0.0,
        )
        g_many_trials = gate_deflated_sharpe(
            **kwargs, n_trials=100, variance_of_trial_sharpes=0.02,
        )
        assert g_many_trials.metric_value <= g_one_trial.metric_value

    def test_returns_gateresult(self):
        g = gate_deflated_sharpe(
            observed_sharpe=1.0, n_observations=252,
            n_trials=1, variance_of_trial_sharpes=0.0,
        )
        assert isinstance(g, GateResult)
        assert g.name == "deflated_sharpe"
        assert g.comparison == ">="


# ─── gate_cv_pass_rate ───────────────────────────────────────────────


class TestGateCVPassRate:
    def test_all_folds_pass(self):
        g = gate_cv_pass_rate(
            fold_sharpes=[1.0, 1.5, 0.8, 2.0, 0.7],
            min_sharpe_per_fold=0.5,
            min_pass_rate=0.7,
        )
        assert g.passed is True
        assert g.metric_value == 1.0

    def test_some_folds_fail_but_pass_rate_clears(self):
        # 4/5 folds at 0.5+; pass_rate = 0.8 > 0.7 → PASS
        g = gate_cv_pass_rate(
            fold_sharpes=[1.0, 1.5, 0.8, 2.0, 0.1],
            min_sharpe_per_fold=0.5,
            min_pass_rate=0.7,
        )
        assert g.passed is True
        assert g.metric_value == 0.8

    def test_too_many_folds_fail(self):
        # 2/5 folds at 0.5+; pass_rate = 0.4 < 0.7 → FAIL
        g = gate_cv_pass_rate(
            fold_sharpes=[1.0, 0.2, 0.3, 0.1, 0.7],
            min_sharpe_per_fold=0.5,
            min_pass_rate=0.7,
        )
        assert g.passed is False
        assert g.metric_value == 0.4

    def test_empty_folds_fails(self):
        g = gate_cv_pass_rate(fold_sharpes=[])
        assert g.passed is False

    def test_nan_folds_dropped(self):
        # Only the finite folds count
        g = gate_cv_pass_rate(
            fold_sharpes=[1.0, np.nan, 0.8, np.inf],
            min_sharpe_per_fold=0.5,
            min_pass_rate=0.7,
        )
        # 2/2 finite folds pass — 1.0 pass rate
        assert g.metric_value == 1.0


# ─── gate_capacity ───────────────────────────────────────────────────


class TestGateCapacity:
    def _make_cap(self, alpha_bps=20.0, adv_usd=10_000_000.0):
        return analyze_capacity(
            alpha_bps=alpha_bps,
            impact_model=SqrtLawImpact(),
            adv_usd=adv_usd,
            daily_vol=0.03,
        )

    def test_sufficient_capacity_passes(self):
        cap = self._make_cap(alpha_bps=20.0, adv_usd=10_000_000.0)
        # Half-alpha size ≈ $22K → easily clears $10K
        g = gate_capacity(cap, min_trade_size_usd=10_000.0)
        assert g.passed is True

    def test_insufficient_capacity_fails(self):
        # Tiny alpha + tiny ADV → tiny capacity
        cap = self._make_cap(alpha_bps=1.0, adv_usd=100_000.0)
        g = gate_capacity(cap, min_trade_size_usd=10_000.0)
        assert g.passed is False

    def test_details_includes_binding_constraint(self):
        cap = self._make_cap()
        g = gate_capacity(cap, min_trade_size_usd=10_000.0)
        assert "binding:" in g.details


# ─── gate_track_record ───────────────────────────────────────────────


class TestGateTrackRecord:
    def test_both_thresholds_clear(self):
        g = gate_track_record(
            n_observations=500, n_trades=200,
            min_observations=252, min_trades=100,
        )
        assert g.passed is True

    def test_observations_too_few(self):
        g = gate_track_record(
            n_observations=100, n_trades=200,
            min_observations=252, min_trades=100,
        )
        assert g.passed is False
        # Should report the failing metric
        assert g.metric_value == 100.0

    def test_trades_too_few(self):
        g = gate_track_record(
            n_observations=500, n_trades=50,
            min_observations=252, min_trades=100,
        )
        assert g.passed is False
        assert g.metric_value == 50.0

    def test_trades_optional(self):
        g = gate_track_record(
            n_observations=300,
            min_observations=252,
            min_trades=None,
            n_trades=None,
        )
        assert g.passed is True

    def test_exactly_at_threshold_passes(self):
        g = gate_track_record(
            n_observations=252, n_trades=100,
            min_observations=252, min_trades=100,
        )
        assert g.passed is True


# ─── gate_max_drawdown ───────────────────────────────────────────────


class TestGateMaxDrawdown:
    def test_shallow_drawdown_passes(self):
        # Returns with -5% drawdown
        returns = np.array([0.01, -0.05, 0.04, 0.01])
        g = gate_max_drawdown(returns, max_dd_pct=0.20)
        assert g.passed is True
        assert g.metric_value > -0.20

    def test_deep_drawdown_fails(self):
        # Construct returns with > 30% drawdown
        returns = np.array([0.05, -0.10, -0.10, -0.15])
        g = gate_max_drawdown(returns, max_dd_pct=0.20)
        assert g.passed is False
        assert g.metric_value < -0.20

    def test_empty_returns_fails(self):
        g = gate_max_drawdown([], max_dd_pct=0.20)
        assert g.passed is False

    def test_threshold_reported_as_negative(self):
        """max_dd_pct=0.20 means threshold = -0.20 in the result."""
        returns = np.array([0.01, 0.02, -0.01])
        g = gate_max_drawdown(returns, max_dd_pct=0.20)
        assert g.threshold == pytest.approx(-0.20)


# ─── evaluate_promotion ──────────────────────────────────────────────


class TestEvaluatePromotion:
    def test_all_pass_overall_passes(self):
        all_passing = [
            GateResult("a", True, 1.0, 0.5, ">="),
            GateResult("b", True, 1.0, 0.5, ">="),
        ]
        v = evaluate_promotion(all_passing)
        assert v.overall_passed is True
        assert v.failures() == []

    def test_one_fail_kills_verdict(self):
        gates = [
            GateResult("a", True, 1.0, 0.5, ">="),
            GateResult("b", False, 0.3, 0.5, ">="),
            GateResult("c", True, 2.0, 0.5, ">="),
        ]
        v = evaluate_promotion(gates)
        assert v.overall_passed is False
        assert len(v.failures()) == 1
        assert v.failures()[0].name == "b"

    def test_empty_gates_fails(self):
        v = evaluate_promotion([])
        assert v.overall_passed is False

    def test_summary_includes_pass_or_fail_label(self):
        gates = [GateResult("a", True, 1.0, 0.5, ">=")]
        s = evaluate_promotion(gates).summary()
        assert "PASS" in s
        gates_fail = [GateResult("a", False, 0.3, 0.5, ">=")]
        s2 = evaluate_promotion(gates_fail).summary()
        assert "FAIL" in s2

    def test_end_to_end_with_real_gates(self):
        """Integration: chain real gate results through evaluate_promotion."""
        rng = np.random.default_rng(0)
        # Strong daily returns: high mean, low vol
        returns = rng.normal(0.002, 0.005, 500)
        fold_sharpes = np.array([1.5, 1.2, 1.8, 1.0, 1.4])
        cap = analyze_capacity(
            alpha_bps=20.0,
            impact_model=SqrtLawImpact(),
            adv_usd=10_000_000.0,
            daily_vol=0.03,
        )

        gates = [
            gate_deflated_sharpe(
                observed_sharpe=returns.mean() / returns.std(),
                n_observations=len(returns),
                n_trials=1,
                variance_of_trial_sharpes=0.0,
            ),
            gate_cv_pass_rate(fold_sharpes),
            gate_capacity(cap, min_trade_size_usd=10_000.0),
            gate_track_record(n_observations=len(returns), n_trades=200),
            gate_max_drawdown(returns, max_dd_pct=0.30),
        ]
        v = evaluate_promotion(gates)
        # Strong synthetic returns + good fold Sharpes + healthy capacity → should pass
        assert v.overall_passed is True
