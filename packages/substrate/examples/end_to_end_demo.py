"""End-to-end RIFT substrate composition demo.

Walks a synthetic 3-asset universe through every major substrate primitive
in turn. The output is a single structured report and a SealedBundle.

This script is the readable proof that the substrate primitives compose
cleanly — each section uses primitives from a different submodule and
hands its output to the next. If the substrate has integration gaps, they
surface here, not in isolated unit tests.

Run:
    PYTHONPATH=engine/src engine/.venv/bin/python3 packages/substrate/examples/end_to_end_demo.py

The script exits 0 if the pipeline runs cleanly (regardless of whether the
strategy promotes — promotion outcome is data-dependent).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from rift_substrate import (
    # Stats
    Stats,
    deflated_sharpe_ratio,
    # Decay
    AlphaDecayCurve,
    compute_ic_curve,
    estimate_half_life,
    make_forward_returns,
    # Purged CV
    PurgedKFold,
    # Backtest (event-driven sanity demo)
    BookEvent,
    EventStrategy,
    ExecutionSimulator,
    L2Level,
    OrderEvent,
    OrderSide,
    TickEvent,
    run_event_driven_backtest,
    # Frictions / capacity
    SqrtLawImpact,
    analyze_capacity,
    # Cross-impact
    basket_impact,
    correlation_matrix,
    # Promotion
    evaluate_promotion,
    gate_capacity,
    gate_cv_pass_rate,
    gate_deflated_sharpe,
    gate_max_drawdown,
    gate_track_record,
    # Provenance
    SealedBundle,
    hash_canonical_json,
    hash_text,
    save_bundle,
)


# ─── Section 1: synthetic 3-asset universe ───────────────────────────


def build_universe(seed: int = 42):
    """Generate 2,000 periods of correlated returns + a latent predictive signal."""
    rng = np.random.default_rng(seed)
    T = 2_000
    asset_names = ["BTC", "ETH", "SOL"]
    advs_usd = np.array([100_000_000.0, 50_000_000.0, 20_000_000.0])
    daily_vols = np.array([0.03, 0.04, 0.06])
    period_vols = daily_vols / np.sqrt(24)  # treat T as hourly bars

    # True correlation structure
    rho_true = np.array([
        [1.00, 0.70, 0.60],
        [0.70, 1.00, 0.65],
        [0.60, 0.65, 1.00],
    ])
    L = np.linalg.cholesky(rho_true)

    # Latent AR(1) signal — separately for each asset, then mildly cross-correlated
    phi = 0.7
    signals = np.zeros((T, 3))
    for j in range(3):
        eps = rng.standard_normal(T)
        for t in range(1, T):
            signals[t, j] = phi * signals[t - 1, j] + np.sqrt(1 - phi**2) * eps[t]

    # Returns: alpha-from-signal + correlated Gaussian noise
    alpha_per_period = 0.0008  # so signal=1 → ~8 bps next-period return
    with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
        noise = rng.standard_normal((T, 3)) @ L.T
    returns = alpha_per_period * signals + noise * period_vols[np.newaxis, :]
    prices = 100.0 * np.cumprod(1 + returns, axis=0)

    return {
        "asset_names": asset_names,
        "advs_usd": advs_usd,
        "daily_vols": daily_vols,
        "T": T,
        "signals": signals,
        "returns": returns,
        "prices": prices,
        "seed": seed,
    }


# ─── Section 2: alpha decay analysis ─────────────────────────────────


def analyze_decay(universe: dict) -> dict:
    """Half-life of the predictive signal for asset 0 (BTC)."""
    prices = universe["prices"][:, 0]
    signal = universe["signals"][:, 0]
    horizons = [1, 2, 5, 10, 20, 50, 100]
    forward_returns = make_forward_returns(prices, horizons)
    curve = compute_ic_curve(
        signal=signal,
        forward_returns=forward_returns,
        horizons=horizons,
        method="spearman",
        n_bootstrap=0,
    )
    fit = estimate_half_life(curve)
    return {"curve": curve, "fit": fit}


# ─── Section 3: purged k-fold cross-validation ───────────────────────


def purged_cv_fold_sharpes(universe: dict) -> list[float]:
    """5-fold purged CV of signal × next-period return on BTC.

    Each fold's "OOS Sharpe" = mean(signal × forward_return) / std(...) over the
    test indices. Forward-label window is 1 period; embargo 1% of the sample.
    """
    signal = universe["signals"][:, 0]
    fwd_ret = universe["returns"][1:, 0]  # next-period return aligned with signal
    signal = signal[:-1]  # truncate so lengths match
    T = signal.size

    # Trivial label intervals: t0=t, t1=t+1 for each sample
    t0 = np.arange(T, dtype=np.float64)
    t1 = t0 + 1

    X = signal.reshape(-1, 1)
    cv = PurgedKFold(n_splits=5, embargo_pct=0.01)
    sharpes = []
    for train_idx, test_idx in cv.split(X, t1, t0):
        # Position = sign of signal (long if positive, short if negative)
        # PnL = position × forward_return
        pos = np.sign(signal[test_idx])
        pnl = pos * fwd_ret[test_idx]
        if pnl.std() == 0:
            sharpes.append(0.0)
            continue
        sharpes.append(float(pnl.mean() / pnl.std() * np.sqrt(24 * 365)))  # annualized hourly
    return sharpes


# ─── Section 4: event-driven backtest (execution sanity demo) ────────


class _SignalMomentumStrategy(EventStrategy):
    """Submit a single BUY then a single SELL to demonstrate execution flow."""

    def __init__(self):
        self.bought = False
        self.sold = False

    def on_tick(self, event, context):
        if not self.bought:
            self.bought = True
            return [OrderEvent(event.timestamp_ms, OrderSide.BUY, size=0.5)]
        if not self.sold and context.position > 0:
            self.sold = True
            return [OrderEvent(event.timestamp_ms, OrderSide.SELL, size=0.5)]
        return []


def run_execution_demo(universe: dict) -> dict:
    """Run a tiny event-driven backtest on BTC to demonstrate the execution layer."""
    prices = universe["prices"][:, 0]
    # Use the first 200 bars to keep the demo fast
    sample = prices[:200]
    events = []
    for t, px in enumerate(sample):
        ts_ms = t * 1_000  # one second per "bar" for the demo
        # Synthetic L2 book: ±5 bps from mid, $1M per side
        spread_bps = 5.0
        bid_px = px * (1 - spread_bps / 10_000)
        ask_px = px * (1 + spread_bps / 10_000)
        depth_base = 1_000_000.0 / px
        events.append(BookEvent(
            timestamp_ms=ts_ms,
            bids=[L2Level(bid_px, depth_base)],
            asks=[L2Level(ask_px, depth_base)],
        ))
        events.append(TickEvent(
            timestamp_ms=ts_ms + 1,
            price=float(px),
            size=0.01,
            side=OrderSide.BUY,
        ))

    result = run_event_driven_backtest(
        strategy=_SignalMomentumStrategy(),
        events=events,
        initial_equity=10_000.0,
        execution_simulator=ExecutionSimulator(latency_ms=50),
    )
    return {"backtest": result}


# ─── Section 5: stats + DSR on the full vectorized backtest ──────────


def compute_strategy_stats(universe: dict) -> dict:
    """Vectorized backtest: position = sign(signal); compute MetricBundle + DSR."""
    signal = universe["signals"][:, 0]
    fwd_ret = universe["returns"][1:, 0]
    signal = signal[:-1]
    pos = np.sign(signal)
    pnl_returns = pos * fwd_ret

    metrics = Stats.from_returns(
        pnl_returns,
        periods_per_year=24 * 365,
        n_bootstrap=200,
        seed=universe["seed"],
    )

    # DSR — no parameter sweep here, so n_trials=1 (DSR reduces to PSR @ threshold=0)
    per_period_sr = pnl_returns.mean() / pnl_returns.std()
    dsr = deflated_sharpe_ratio(
        observed_sharpe=per_period_sr,
        n_observations=len(pnl_returns),
        n_trials=1,
        variance_of_trial_sharpes=0.0,
    )

    return {
        "metrics": metrics,
        "dsr": float(dsr),
        "per_period_sharpe": float(per_period_sr),
        "returns": pnl_returns,
    }


# ─── Section 6: capacity analysis ────────────────────────────────────


def compute_capacity(universe: dict, stats_section: dict) -> dict:
    """Capacity for BTC strategy with sqrt-law impact + ADV + a synthetic L2 book."""
    # Alpha per trade in bps — use the per-period return mean as a proxy
    pnl_returns = stats_section["returns"]
    # Cast positive-side magnitude in bps
    alpha_bps = max(1.0, abs(pnl_returns.mean()) * 10_000.0)

    impact = SqrtLawImpact(gamma=0.7)
    # Synthetic L2 book: 3 levels, 1% slip across the visible depth
    book = [
        L2Level(price=100.0, size=10_000.0),
        L2Level(price=100.5, size=20_000.0),
        L2Level(price=101.0, size=40_000.0),
    ]
    cap = analyze_capacity(
        alpha_bps=alpha_bps,
        impact_model=impact,
        adv_usd=universe["advs_usd"][0],
        daily_vol=universe["daily_vols"][0],
        book_side=book,
        mid_price=100.0,
        side="buy",
        max_slippage_bps=20.0,
    )
    return {"capacity": cap, "alpha_bps_used": alpha_bps}


# ─── Section 7: cross-impact for a hedged basket ─────────────────────


def analyze_cross_impact(universe: dict) -> dict:
    """Demonstrate basket execution for an aligned trio and a hedged pair."""
    impact = SqrtLawImpact(gamma=0.7)
    rho = correlation_matrix(universe["returns"])

    # Aligned basket: long all three
    aligned = basket_impact(
        trades_usd=[50_000.0, 50_000.0, 50_000.0],
        correlations=rho,
        advs_usd=universe["advs_usd"],
        daily_vols=universe["daily_vols"],
        impact_model=impact,
        asset_names=universe["asset_names"],
    )
    # Hedged: long BTC, short ETH, flat SOL
    hedged = basket_impact(
        trades_usd=[50_000.0, -50_000.0, 0.0],
        correlations=rho,
        advs_usd=universe["advs_usd"],
        daily_vols=universe["daily_vols"],
        impact_model=impact,
        asset_names=universe["asset_names"],
    )
    return {"rho": rho, "aligned": aligned, "hedged": hedged}


# ─── Section 8: promotion verdict ────────────────────────────────────


def build_promotion_verdict(
    stats_section: dict,
    fold_sharpes: list[float],
    capacity_section: dict,
):
    """Compose every gate into one verdict."""
    metrics = stats_section["metrics"]
    returns = stats_section["returns"]

    gates = [
        gate_deflated_sharpe(
            observed_sharpe=stats_section["per_period_sharpe"],
            n_observations=len(returns),
            n_trials=1,
            variance_of_trial_sharpes=0.0,
            min_dsr=0.95,
        ),
        gate_cv_pass_rate(
            fold_sharpes=fold_sharpes,
            min_sharpe_per_fold=0.0,  # any positive OOS Sharpe counts
            min_pass_rate=0.6,
        ),
        gate_capacity(
            capacity_result=capacity_section["capacity"],
            min_trade_size_usd=1_000.0,
        ),
        gate_track_record(
            n_observations=len(returns),
            n_trades=int((np.diff(np.sign(stats_section["returns"])) != 0).sum()),
            min_observations=500,
            min_trades=100,
        ),
        gate_max_drawdown(
            returns=returns,
            max_dd_pct=0.30,
        ),
    ]
    return evaluate_promotion(gates)


# ─── Section 9: seal the bundle ──────────────────────────────────────


def seal(
    universe: dict,
    decay: dict,
    fold_sharpes: list[float],
    stats_section: dict,
    capacity_section: dict,
    cross_impact_section: dict,
    verdict,
    out_dir: Path,
) -> Path:
    """Build a SealedBundle hashing all of the above and write to out_dir."""
    config = {
        "asset_names": universe["asset_names"],
        "T": universe["T"],
        "seed": universe["seed"],
        "advs_usd": universe["advs_usd"].tolist(),
        "daily_vols": universe["daily_vols"].tolist(),
    }
    result = {
        "half_life": float(decay["fit"].half_life)
            if np.isfinite(decay["fit"].half_life) else None,
        "cv_fold_sharpes": fold_sharpes,
        "dsr": stats_section["dsr"],
        "metrics_sharpe": float(stats_section["metrics"].sharpe),
        "metrics_max_drawdown": float(stats_section["metrics"].max_drawdown),
        "capacity_max_trade_usd": float(capacity_section["capacity"].max_trade_size_usd),
        "capacity_binding": capacity_section["capacity"].binding_constraint,
        "aligned_basket_cost": float(cross_impact_section["aligned"].total_cost_usd),
        "hedged_basket_cost": float(cross_impact_section["hedged"].total_cost_usd),
        "promotion_passed": verdict.overall_passed,
        "promotion_failures": [g.name for g in verdict.failures()],
    }

    # Hash a synthetic "data_hash" since the data is generated here
    data_repr = f"synthetic_universe(T={universe['T']},seed={universe['seed']})"
    bundle = SealedBundle.from_inputs(
        bundle_type="custom",
        data_hash=hash_text(data_repr),
        config_hash=hash_canonical_json(config),
        result_hash=hash_canonical_json(result),
        code_hash=hash_text(__file__) if Path(__file__).exists() else "",
        rng_seed=universe["seed"],
        metadata={
            "demo": "end_to_end_demo",
            "strategy": "sign(signal) momentum on BTC",
        },
    )
    return save_bundle(bundle, bundles_dir=out_dir)


# ─── Section 10: orchestrate + report ────────────────────────────────


def render_report(
    universe: dict,
    decay: dict,
    fold_sharpes: list[float],
    stats_section: dict,
    capacity_section: dict,
    cross_impact_section: dict,
    execution_demo: dict,
    verdict,
    bundle_path: Path,
) -> str:
    metrics = stats_section["metrics"]
    fit = decay["fit"]
    cap = capacity_section["capacity"]
    aligned = cross_impact_section["aligned"]
    hedged = cross_impact_section["hedged"]
    btest = execution_demo["backtest"]

    lines = []
    lines.append("=" * 70)
    lines.append("RIFT End-to-End Substrate Demo  —  synthetic 3-asset universe")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"Universe: {universe['asset_names']}, T={universe['T']}, seed={universe['seed']}")
    lines.append("")
    lines.append("─── Alpha decay (BTC signal) ─────────────────────────────────────")
    lines.append(f"  Half-life: {fit.half_life:.2f} periods")
    lines.append(f"  τ:         {fit.tau:.2f}")
    lines.append(f"  IC₀:       {fit.ic_initial:+.4f}")
    lines.append(f"  R² fit:    {fit.r_squared:.3f}")
    lines.append("")
    lines.append("─── Purged 5-fold CV ─────────────────────────────────────────────")
    lines.append(f"  Fold OOS Sharpes (annualized): {[f'{x:+.2f}' for x in fold_sharpes]}")
    lines.append(f"  Pass rate (>0): {np.mean([s > 0 for s in fold_sharpes]):.0%}")
    lines.append("")
    lines.append("─── Vectorized backtest stats ────────────────────────────────────")
    lines.append(f"  Sharpe (annualized):  {metrics.sharpe:+.2f}    95% CI: [{metrics.sharpe_ci_95[0]:+.2f}, {metrics.sharpe_ci_95[1]:+.2f}]")
    lines.append(f"  Sortino:              {metrics.sortino:+.2f}")
    lines.append(f"  Max DD:               {metrics.max_drawdown:+.2%}")
    lines.append(f"  DSR (n_trials=1):     {stats_section['dsr']:.3f}")
    lines.append("")
    lines.append("─── Event-driven execution sanity ────────────────────────────────")
    lines.append(f"  Round trip: {btest.num_fills} fills, fees ${btest.total_fees_usd:.2f}")
    lines.append(f"  PnL on $10K seed:     ${btest.total_pnl:+.2f}")
    lines.append("")
    lines.append("─── Capacity analysis (BTC) ──────────────────────────────────────")
    lines.append(f"  Alpha used:           {capacity_section['alpha_bps_used']:.2f} bps")
    lines.append(f"  Max trade size:       ${cap.max_trade_size_usd:,.0f}  (binding: {cap.binding_constraint})")
    lines.append(f"  Half-alpha size:      ${cap.half_alpha_size_usd:,.0f}")
    lines.append(f"  Breakeven size:       ${cap.breakeven_size_usd:,.0f}")
    lines.append("")
    lines.append("─── Cross-impact comparison ──────────────────────────────────────")
    lines.append(f"  Aligned basket cost:  ${aligned.total_cost_usd:+,.2f}  (cross-term: ${aligned.cross_term_usd:+,.2f})")
    lines.append(f"  Hedged pair cost:     ${hedged.total_cost_usd:+,.2f}  (cross-term: ${hedged.cross_term_usd:+,.2f})")
    lines.append(f"  Hedged < aligned: {hedged.total_cost_usd < aligned.total_cost_usd}  ← cross-impact relief on the hedge")
    lines.append("")
    lines.append("─── Promotion verdict ────────────────────────────────────────────")
    lines.append(verdict.summary())
    lines.append("")
    lines.append("─── SealedBundle ─────────────────────────────────────────────────")
    lines.append(f"  Path: {bundle_path}")
    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def run_demo(verbose: bool = True) -> dict:
    """Execute the full pipeline. Returns the assembled result dict.

    Importable by tests; prints the report only when verbose=True.
    """
    universe = build_universe(seed=42)
    decay = analyze_decay(universe)
    fold_sharpes = purged_cv_fold_sharpes(universe)
    execution_demo = run_execution_demo(universe)
    stats_section = compute_strategy_stats(universe)
    capacity_section = compute_capacity(universe, stats_section)
    cross_impact_section = analyze_cross_impact(universe)
    verdict = build_promotion_verdict(stats_section, fold_sharpes, capacity_section)

    with tempfile.TemporaryDirectory() as tmp:
        bundle_path = seal(
            universe, decay, fold_sharpes, stats_section,
            capacity_section, cross_impact_section, verdict, Path(tmp),
        )
        # Read it back to assert round-trip works
        bundle_text = bundle_path.read_text()
        bundle_dict = json.loads(bundle_text)

        if verbose:
            print(render_report(
                universe, decay, fold_sharpes, stats_section,
                capacity_section, cross_impact_section, execution_demo,
                verdict, bundle_path,
            ))

    return {
        "universe": universe,
        "decay": decay,
        "fold_sharpes": fold_sharpes,
        "stats": stats_section,
        "capacity": capacity_section,
        "cross_impact": cross_impact_section,
        "execution_demo": execution_demo,
        "verdict": verdict,
        "bundle": bundle_dict,
    }


if __name__ == "__main__":
    run_demo(verbose=True)
