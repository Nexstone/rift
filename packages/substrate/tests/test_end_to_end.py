"""Integration test for the end-to-end substrate composition demo.

The demo script in `packages/substrate/examples/end_to_end_demo.py` chains
every major substrate primitive into one workflow. This test runs that
chain and asserts:

  1. The script completes without raising.
  2. Each section produces an output of the expected shape/type.
  3. The SealedBundle round-trips as valid JSON with the expected keys.
  4. Cross-impact relief direction is correct (hedged < aligned).
  5. The promotion verdict's gate breakdown is internally consistent.

This test catches composition regressions — e.g., a primitive's dataclass
field gets renamed in isolation and the unit tests still pass, but the
chain breaks at the boundary. The unit tests verify isolated correctness;
this test verifies the system holds together.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

# Import the demo module by path so tests don't depend on packaging the
# examples directory.
_DEMO_PATH = (
    Path(__file__).resolve().parents[1] / "examples" / "end_to_end_demo.py"
)

_spec = importlib.util.spec_from_file_location("end_to_end_demo", _DEMO_PATH)
assert _spec is not None and _spec.loader is not None
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


# ─── End-to-end smoke ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def demo_result():
    """Run the demo once for the whole test module; share across tests."""
    return demo.run_demo(verbose=False)


class TestDemoChainCompletes:
    def test_demo_runs_without_error(self, demo_result):
        """The whole pipeline executed and returned a dict of sections."""
        assert isinstance(demo_result, dict)
        for key in [
            "universe",
            "decay",
            "fold_sharpes",
            "stats",
            "capacity",
            "cross_impact",
            "execution_demo",
            "verdict",
            "bundle",
        ]:
            assert key in demo_result, f"missing section: {key}"


# ─── Section outputs are well-shaped ────────────────────────────────


class TestSectionShapes:
    def test_universe_is_3_asset(self, demo_result):
        u = demo_result["universe"]
        assert u["asset_names"] == ["BTC", "ETH", "SOL"]
        assert u["returns"].shape == (u["T"], 3)
        assert u["signals"].shape == (u["T"], 3)
        assert u["prices"].shape == (u["T"], 3)

    def test_decay_curve_and_fit_present(self, demo_result):
        d = demo_result["decay"]
        assert d["curve"].horizons.size > 0
        # half_life may be inf or NaN depending on data, but must exist
        assert hasattr(d["fit"], "half_life")
        assert hasattr(d["fit"], "tau")

    def test_cv_fold_sharpes_are_5(self, demo_result):
        assert len(demo_result["fold_sharpes"]) == 5
        # All finite
        for s in demo_result["fold_sharpes"]:
            assert np.isfinite(s)

    def test_stats_section_has_metrics_and_dsr(self, demo_result):
        s = demo_result["stats"]
        assert hasattr(s["metrics"], "sharpe")
        assert hasattr(s["metrics"], "max_drawdown")
        assert np.isfinite(s["dsr"])
        assert 0.0 <= s["dsr"] <= 1.0

    def test_execution_demo_round_trip(self, demo_result):
        ed = demo_result["execution_demo"]
        bt = ed["backtest"]
        # The dummy strategy buys then sells — expect 2 fills, flat final position
        assert bt.num_fills == 2
        assert abs(bt.final_position) < 1e-9

    def test_capacity_has_binding_constraint(self, demo_result):
        cap = demo_result["capacity"]["capacity"]
        assert cap.binding_constraint in ("impact", "adv", "l2_depth")
        assert cap.max_trade_size_usd > 0
        # The capacity curve has the configured number of points
        assert len(cap.capacity_curve) > 0


# ─── Cross-impact relief direction ──────────────────────────────────


class TestCrossImpactRelief:
    def test_hedged_basket_cheaper_than_aligned(self, demo_result):
        """Pairs trade should be cheaper than same-direction basket
        on a correlated universe — the canonical cross-impact result."""
        aligned = demo_result["cross_impact"]["aligned"]
        hedged = demo_result["cross_impact"]["hedged"]
        assert hedged.total_cost_usd < aligned.total_cost_usd
        # Aligned should have positive cross-term (extra cost from co-impact)
        assert aligned.cross_term_usd > 0
        # Hedged should have negative cross-term (cross-impact relief)
        assert hedged.cross_term_usd < 0


# ─── Promotion verdict consistency ──────────────────────────────────


class TestPromotionVerdict:
    def test_verdict_has_5_gates(self, demo_result):
        v = demo_result["verdict"]
        assert len(v.gate_results) == 5
        # The 5 gates we wired up
        names = {g.name for g in v.gate_results}
        assert names == {
            "deflated_sharpe",
            "cv_pass_rate",
            "capacity",
            "track_record",
            "max_drawdown",
        }

    def test_overall_passed_consistent_with_gates(self, demo_result):
        v = demo_result["verdict"]
        all_passed = all(g.passed for g in v.gate_results)
        assert v.overall_passed == all_passed


# ─── SealedBundle round-trip ────────────────────────────────────────


class TestSealedBundleRoundTrip:
    def test_bundle_has_required_fields(self, demo_result):
        b = demo_result["bundle"]
        for field in [
            "bundle_id",
            "bundle_type",
            "created_at_iso",
            "data_hash",
            "config_hash",
            "result_hash",
        ]:
            assert field in b, f"bundle missing field {field}"

    def test_bundle_id_nonempty(self, demo_result):
        assert len(demo_result["bundle"]["bundle_id"]) > 0
