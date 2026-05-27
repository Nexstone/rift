"""Tests for the substrate-based tearsheet renderer.

Pins the new substrate.stats-driven generate_tearsheet behaviour after
ripping out quantstats. The tearsheet output is Markdown; tests check
both structural shape (sections present, paths returned) and substance
(bootstrap CIs, PSR/DSR computed, drawdown analysis correct).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from rift_research.reports import (
    _drawdown_analysis,
    _trade_stats,
    generate_tearsheet,
)


def _synthetic_equity_curve(
    n: int = 500,
    mean_return: float = 0.0005,
    vol: float = 0.01,
    seed: int = 0,
) -> list[float]:
    """Build a synthetic equity curve from drift + Gaussian noise."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(mean_return, vol, n)
    eq = 10_000.0 * np.cumprod(1 + returns)
    return [10_000.0] + list(eq)


# ─── _drawdown_analysis ───────────────────────────────────────────────


class TestDrawdownAnalysis:
    def test_no_drawdown_when_monotonic(self):
        eq = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        info = _drawdown_analysis(eq)
        assert info["max_dd"] == 0.0
        assert info["time_in_drawdown"] == 0.0
        assert info["n_dd_over_5pct"] == 0

    def test_single_drawdown(self):
        # Peak at 100, trough at 80 → 20% drawdown
        eq = np.array([100.0, 90.0, 80.0, 90.0, 100.0])
        info = _drawdown_analysis(eq)
        assert info["max_dd"] == pytest.approx(-0.20)
        assert info["max_dd_trough_idx"] == 2
        assert info["max_dd_duration"] == 2  # peak idx 0 → trough idx 2
        assert info["n_dd_over_5pct"] == 1

    def test_short_input(self):
        info = _drawdown_analysis(np.array([100.0]))
        assert info["max_dd"] == 0.0

    def test_counts_distinct_deep_drawdowns(self):
        # Two separate >5% drawdowns: 100→90 (10%), back to 110, then 110→100 (~9%)
        eq = np.array([100.0, 90.0, 100.0, 110.0, 100.0, 110.0])
        info = _drawdown_analysis(eq)
        assert info["n_dd_over_5pct"] == 2


# ─── _trade_stats ─────────────────────────────────────────────────────


class TestTradeStats:
    def test_empty(self):
        info = _trade_stats([])
        assert info["n"] == 0
        assert info["win_rate"] == 0.0

    def test_all_wins(self):
        info = _trade_stats([0.01, 0.02, 0.03])
        assert info["wins"] == 3
        assert info["losses"] == 0
        assert info["win_rate"] == 1.0
        assert info["avg_win"] == pytest.approx(0.02)
        assert info["profit_factor"] == float("inf")

    def test_mixed(self):
        info = _trade_stats([0.05, -0.02, 0.03, -0.01, 0.04])
        assert info["wins"] == 3
        assert info["losses"] == 2
        assert info["win_rate"] == 0.6
        # profit_factor = sum(wins) / -sum(losses) = 0.12 / 0.03 = 4.0
        assert info["profit_factor"] == pytest.approx(4.0)
        assert info["best"] == 0.05
        assert info["worst"] == -0.02


# ─── generate_tearsheet ───────────────────────────────────────────────


class TestGenerateTearsheet:
    def test_short_input_returns_empty(self, tmp_path):
        path = generate_tearsheet(
            equity_curve=[100.0, 101.0],
            strategy_name="too_short",
            output_dir=tmp_path,
        )
        assert path == ""

    def test_generates_markdown_file(self, tmp_path):
        eq = _synthetic_equity_curve(n=500, mean_return=0.0005, vol=0.01)
        path = generate_tearsheet(
            equity_curve=eq,
            strategy_name="test_strat",
            output_dir=tmp_path,
            interval="1h",
            seed=42,
        )
        assert path != ""
        out = Path(path)
        assert out.exists()
        assert out.suffix == ".md"
        assert out.parent == tmp_path

    def test_tearsheet_contains_expected_sections(self, tmp_path):
        eq = _synthetic_equity_curve(n=500)
        path = generate_tearsheet(eq, "section_check", tmp_path, interval="1h", seed=0)
        text = Path(path).read_text()
        assert "# RIFT Tearsheet — section_check" in text
        assert "## Performance Metrics" in text
        assert "## Statistical Significance" in text
        assert "## Drawdown Analysis" in text
        # CIs were computed
        assert "95% CI" in text
        # PSR is always present (no n_trials gate)
        assert "PSR" in text

    def test_dsr_included_only_when_n_trials_gt_1(self, tmp_path):
        eq = _synthetic_equity_curve(n=400)
        # n_trials = 1 → no DSR
        path1 = generate_tearsheet(eq, "no_dsr", tmp_path, n_trials=1, seed=1)
        assert "not applicable" in Path(path1).read_text().lower()

        # n_trials = 20 → DSR computed
        path2 = generate_tearsheet(
            eq, "with_dsr", tmp_path, n_trials=20,
            variance_of_trial_sharpes=0.05, seed=2,
        )
        text2 = Path(path2).read_text()
        assert "deflated for 20 trial candidates" in text2.lower()

    def test_trade_stats_section_when_provided(self, tmp_path):
        eq = _synthetic_equity_curve(n=400)
        trade_rets = [0.02, -0.01, 0.03, -0.005, 0.015]
        path = generate_tearsheet(
            eq, "with_trades", tmp_path,
            trade_returns=trade_rets, seed=3,
        )
        text = Path(path).read_text()
        assert "## Trade-Level Stats" in text
        assert "Total trades" in text

    def test_no_trade_stats_section_when_omitted(self, tmp_path):
        eq = _synthetic_equity_curve(n=400)
        path = generate_tearsheet(eq, "no_trades", tmp_path, seed=4)
        text = Path(path).read_text()
        assert "## Trade-Level Stats" not in text

    def test_reproducible_with_same_seed(self, tmp_path):
        eq = _synthetic_equity_curve(n=400)
        p1 = generate_tearsheet(eq, "seed_a", tmp_path / "a", seed=7)
        p2 = generate_tearsheet(eq, "seed_a", tmp_path / "b", seed=7)
        text1 = Path(p1).read_text()
        text2 = Path(p2).read_text()
        # Body should match modulo the timestamp line
        # Strip lines starting with "_Generated:"
        def _strip_ts(s):
            return "\n".join(ln for ln in s.split("\n") if not ln.startswith("_Generated:"))
        assert _strip_ts(text1) == _strip_ts(text2)

    def test_uses_interval_for_annualization(self, tmp_path):
        eq = _synthetic_equity_curve(n=400)
        path_hourly = generate_tearsheet(eq, "h", tmp_path, interval="1h", seed=8)
        path_daily = generate_tearsheet(eq, "d", tmp_path, interval="1d", seed=8)
        text_h = Path(path_hourly).read_text()
        text_d = Path(path_daily).read_text()
        # Different periods/year → different annualized return reported
        # (Same returns series, different scaling)
        assert "Periods/year: 8760" in text_h
        assert "Periods/year: 365" in text_d
