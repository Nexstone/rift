"""Tests for rift_research.advanced — substrate-driven post-backtest validations.

Two layers:

  1. **Unit-level** (always runs): feeds `run_advanced_validations()` a
     synthetic BacktestResult + Polars DataFrame and verifies every section
     comes back populated. No data dependency.

  2. **Integration smoke** (skipped when data isn't available): runs the
     full `run_research_pipeline()` against the trend_follow reference
     strategy and asserts the new keys appear in the result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl
import pytest

# Add package src paths
_repo_root = Path(__file__).resolve().parents[3]
for sub in ("engine/src", "packages/engine/src", "packages/research/src"):
    p = str(_repo_root / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from rift_engine.backtest import BacktestResult, Trade
from rift_research.advanced import run_advanced_validations


# ─── Helpers ────────────────────────────────────────────────────────


def _make_synthetic_backtest(
    n_bars: int = 500,
    n_trades: int = 80,
    seed: int = 42,
) -> tuple[BacktestResult, pl.DataFrame]:
    """Build a plausible BacktestResult + candle DataFrame for testing."""
    rng = np.random.default_rng(seed)
    # Candle data
    start_ts = 1_700_000_000_000
    interval_ms = 60 * 60 * 1000  # 1h
    timestamps = (start_ts + np.arange(n_bars) * interval_ms).astype(np.int64)
    closes = 100.0 * np.cumprod(1 + 0.005 * rng.standard_normal(n_bars))
    opens = closes * (1 + 0.0005 * rng.standard_normal(n_bars))
    highs = np.maximum(opens, closes) * (1 + 0.002 * np.abs(rng.standard_normal(n_bars)))
    lows = np.minimum(opens, closes) * (1 - 0.002 * np.abs(rng.standard_normal(n_bars)))
    volumes = 1000.0 * np.abs(rng.standard_normal(n_bars)) + 500.0

    df = pl.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })

    # Trades with positive expected PnL (so the strategy has a real edge)
    trades = []
    max_idx = max(1, n_bars - 10)
    n_trades_actual = min(n_trades, max_idx)
    trade_idxs = sorted(rng.choice(max_idx, size=n_trades_actual, replace=False))
    for i, idx in enumerate(trade_idxs):
        entry_ts = int(timestamps[idx])
        exit_ts = int(timestamps[min(idx + 5, n_bars - 1)])
        side = "long" if i % 2 == 0 else "short"
        entry_price = float(closes[idx])
        # Slightly biased toward winners
        pnl_pct = float(rng.normal(0.3, 0.8))
        exit_price = entry_price * (1 + pnl_pct / 100.0)
        if side == "short":
            exit_price = entry_price * (1 - pnl_pct / 100.0)
        size = 100.0
        pnl = pnl_pct / 100.0 * entry_price * size
        trades.append(Trade(
            entry_time=entry_ts,
            exit_time=exit_ts,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            size=size,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason="signal",
        ))

    # Equity curve from PnL accrual
    initial_equity = 10_000.0
    equity_curve = [initial_equity]
    rng_noise = rng.normal(0, 5, n_bars - 1)
    for i in range(n_bars - 1):
        eq = equity_curve[-1] * (1 + rng.normal(0.0005, 0.01))
        equity_curve.append(max(100.0, eq))
    equity_curve = list(np.array(equity_curve, dtype=float))

    bt = BacktestResult(
        strategy_name="synth_strategy",
        pair="BTC",
        interval="1h",
        start_time=int(timestamps[0]),
        end_time=int(timestamps[-1]),
        initial_equity=initial_equity,
        final_equity=float(equity_curve[-1]),
        total_return_pct=(equity_curve[-1] / initial_equity - 1) * 100,
        num_trades=len(trades),
        win_rate=float(sum(1 for t in trades if t.pnl > 0) / len(trades) * 100),
        avg_win_pct=float(np.mean([t.pnl_pct for t in trades if t.pnl > 0])),
        avg_loss_pct=float(np.mean([t.pnl_pct for t in trades if t.pnl <= 0])),
        max_drawdown_pct=-12.0,
        sharpe_ratio=1.5,
        profit_factor=1.8,
        equity_curve=equity_curve,
        trades=trades,
    )
    return bt, df


class _DummyStrategyCls:
    """Bare class to play the `strategy_cls` role — no real strategy needed for advanced phase."""
    promotion_gates = {
        "min_dsr": 0.5,             # generous so synth data can pass
        "min_cv_pass_rate": 0.4,
        "min_capacity_usd": 1_000.0,
        "min_observations": 100,
        "min_trades": 50,
        "max_dd_pct": 0.40,
    }


# ─── Unit-level tests ───────────────────────────────────────────────


class TestAdvancedValidations:
    @pytest.fixture(scope="class")
    def result(self):
        bt, df = _make_synthetic_backtest()
        return run_advanced_validations(
            bt=bt,
            df=df,
            strategy_cls=_DummyStrategyCls,
            strategy_name="synth_strategy",
            pair="BTC",
            interval="1h",
            multi_pair_results=None,
            config_overrides=None,
            seed=42,
            emit_fn=None,
        )

    def test_returns_all_six_sections(self, result):
        for key in [
            "purged_cv", "alpha_decay", "capacity",
            "cross_impact", "promotion_verdict", "sealed_bundle",
        ]:
            assert key in result, f"missing section: {key}"

    def test_purged_cv_populated(self, result):
        cv = result["purged_cv"]
        assert cv["status"] == "ok"
        assert cv["n_splits"] == 5
        assert "fold_sharpes_per_period" in cv
        assert len(cv["fold_sharpes_per_period"]) == 5

    def test_alpha_decay_populated(self, result):
        ad = result["alpha_decay"]
        assert ad["status"] == "ok"
        assert "horizons" in ad
        assert len(ad["horizons"]) == len(ad["ics"])

    def test_capacity_populated(self, result):
        c = result["capacity"]
        assert c["status"] == "ok"
        assert c["max_trade_size_usd"] > 0
        assert c["binding_constraint"] in ("impact", "adv", "l2_depth")

    def test_cross_impact_skipped_for_single_asset(self, result):
        """Single-asset run should skip cross-impact cleanly, not error."""
        ci = result["cross_impact"]
        assert ci["status"] == "skipped"
        assert "reason" in ci

    def test_promotion_verdict_complete(self, result):
        pv = result["promotion_verdict"]
        assert pv["status"] == "ok"
        assert "passed" in pv
        assert "gates" in pv
        # Track record + max DD always run; the other three are conditional but
        # with our synthetic data they all should
        gate_names = {g["name"] for g in pv["gates"]}
        # At minimum DSR, track_record, max_drawdown always fire
        assert "deflated_sharpe" in gate_names
        assert "track_record" in gate_names
        assert "max_drawdown" in gate_names
        # CV + capacity ran successfully so their gates should be in there
        assert "cv_pass_rate" in gate_names
        assert "capacity" in gate_names

    def test_promotion_verdict_consistency(self, result):
        pv = result["promotion_verdict"]
        gate_passes = [g["passed"] for g in pv["gates"]]
        assert pv["passed"] == all(gate_passes)
        assert pv["n_passed"] == sum(gate_passes)
        assert pv["n_gates"] == len(gate_passes)

    def test_sealed_bundle_created(self, result):
        sb = result["sealed_bundle"]
        assert sb["status"] == "ok"
        assert "bundle_id" in sb and len(sb["bundle_id"]) > 0
        assert "path" in sb
        # File must exist
        assert Path(sb["path"]).exists()


class TestStrategyDefaults:
    """Strategy with no `promotion_gates` declared must still get defaults."""

    def test_runs_with_no_user_gates(self):
        bt, df = _make_synthetic_backtest()

        class _NoGates:
            pass  # no promotion_gates attribute

        result = run_advanced_validations(
            bt=bt, df=df, strategy_cls=_NoGates,
            strategy_name="s", pair="BTC", interval="1h",
        )
        # Should still produce the promotion verdict using defaults
        assert result["promotion_verdict"]["status"] == "ok"


class TestEdgeCases:
    def test_empty_trades_does_not_crash(self):
        bt, df = _make_synthetic_backtest()
        bt.trades = []
        bt.num_trades = 0
        result = run_advanced_validations(
            bt=bt, df=df, strategy_cls=_DummyStrategyCls,
            strategy_name="s", pair="BTC", interval="1h",
        )
        # Capacity should skip (no trades to measure alpha)
        assert result["capacity"]["status"] == "skipped"
        # Promotion still runs (using equity-curve returns)
        # — verdict might fail but the section is populated
        assert result["promotion_verdict"]["status"] in ("ok", "skipped")

    def test_very_short_equity_curve_skips_cv(self):
        """< 20 period returns → purged CV refuses to split."""
        bt, df = _make_synthetic_backtest(n_bars=20, n_trades=2)
        result = run_advanced_validations(
            bt=bt, df=df, strategy_cls=_DummyStrategyCls,
            strategy_name="s", pair="BTC", interval="1h",
        )
        assert result["purged_cv"]["status"] == "skipped"


# ─── Integration smoke test (data-dependent) ────────────────────────


def _data_and_strategy_available() -> tuple[bool, str]:
    """Return (available, reason)."""
    try:
        sdk_examples = (
            _repo_root / "packages/strategies-sdk/src/rift_strategies_sdk/examples"
        )
        sys.path.insert(0, str(_repo_root / "packages/strategies-sdk/src"))
        from rift_engine.strategy import discover_strategies, get_strategy
        discover_strategies([sdk_examples])
        get_strategy("trend_follow")
    except Exception as exc:
        return False, f"trend_follow not discoverable: {exc}"
    try:
        from rift_data.historical import load_candles_smart
        df = load_candles_smart("BTC", "4h")
        if df is None or len(df) < 200:
            return False, "BTC 4h data not available or too short"
    except Exception as exc:
        return False, f"data load failed: {exc}"
    return True, "ok"


_DATA_OK, _DATA_REASON = _data_and_strategy_available()


class TestIntegrationSmoke:
    """Real-data run of the extended pipeline against the OSS reference strategy.

    Strategy-agnostic by construction: trend_follow is unmodified — the
    new sections appear automatically because the runner wires the substrate
    advanced phase for every strategy.
    """

    @pytest.mark.skipif(not _DATA_OK, reason=_DATA_REASON)
    def test_full_pipeline_on_trend_follow(self):
        from rift_research.research import run_research_pipeline
        result = run_research_pipeline(
            strategy_name="trend_follow",
            pair="BTC",
            interval="4h",
            strategies_dir=str(
                _repo_root / "packages/strategies-sdk/src/rift_strategies_sdk/examples"
            ),
        )

        # New sections all present (strategy-agnostic wiring)
        for key in [
            "purged_cv", "alpha_decay", "capacity",
            "cross_impact", "promotion_verdict", "sealed_bundle",
        ]:
            assert key in result, f"missing section: {key}"

        # Each section has a status field
        for key in ["purged_cv", "alpha_decay", "capacity",
                    "cross_impact", "promotion_verdict", "sealed_bundle"]:
            assert "status" in result[key], f"section {key} missing status"

        # Promotion verdict and sealed bundle should always reach ok status
        # if backtest produced trades (trend_follow is known to trade)
        if result["backtest"]["num_trades"] > 0:
            assert result["promotion_verdict"]["status"] == "ok"
            assert result["sealed_bundle"]["status"] == "ok"

        # Existing fields unchanged (backward-compatible)
        assert "grade" in result
        assert "verdict" in result
        assert "backtest" in result
