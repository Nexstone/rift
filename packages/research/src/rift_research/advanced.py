"""Advanced strategy validations — wires the newer substrate modules into the
research pipeline.

`run_advanced_validations()` runs as a strategy-agnostic post-backtest layer:

    purged k-fold CV   — rift_substrate.validation
    alpha decay        — rift_substrate.decay
    capacity analysis  — rift_substrate.capacity
    cross-impact       — rift_substrate.cross_impact   (multi-asset only)
    promotion verdict  — rift_substrate.promotion
    sealed bundle      — rift_substrate.provenance

Each phase has its own try/except so a failure in one doesn't kill the
others. Each section's output is a plain dict (no substrate dataclasses
leak into the caller's return shape — keeps the runner's wire format
JSON-friendly and stable).

Thresholds for the promotion gates are read off the strategy class via
`getattr(strategy_cls, "promotion_gates", {})`. Strategies that don't
declare gates get the defaults below. This is strategy-agnostic by
construction: the runner asks the strategy what it wants, falling back
to industry-standard defaults if the strategy is silent.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from rift_substrate import (
    PurgedKFold,
    SqrtLawImpact,
    analyze_capacity,
    basket_impact,
    compute_ic_curve,
    correlation_matrix,
    deflated_sharpe_ratio,
    estimate_half_life,
    evaluate_promotion,
    gate_capacity,
    gate_cv_pass_rate,
    gate_deflated_sharpe,
    gate_max_drawdown,
    gate_track_record,
    hash_canonical_json,
    hash_text,
    make_forward_returns,
    periods_per_year_for_interval,
    save_bundle,
    SealedBundle,
)


# ─── Default promotion thresholds ────────────────────────────────────


_DEFAULT_GATES: dict[str, Any] = {
    "min_dsr": 0.95,
    "min_cv_pass_rate": 0.7,
    "min_sharpe_per_fold": 0.0,
    "min_capacity_usd": 10_000.0,
    "min_observations": 252,
    "min_trades": 100,
    "max_dd_pct": 0.30,
    "max_impact_fraction": 0.5,
    "max_adv_pct": 0.05,
}


# ─── Helpers ────────────────────────────────────────────────────────


def _period_returns_from_equity_curve(equity_curve: list[float]) -> np.ndarray:
    arr = np.asarray(equity_curve, dtype=np.float64)
    arr = arr[arr > 0]  # drop any pre-init zeros
    if arr.size < 2:
        return np.array([], dtype=np.float64)
    return np.diff(arr) / arr[:-1]


def _position_series_from_trades(trades: list, df_timestamps: np.ndarray) -> np.ndarray:
    """Reconstruct a per-bar position series from completed trades.

    +1 when long, -1 when short, 0 when flat. Aligned with df_timestamps.
    """
    pos = np.zeros(df_timestamps.size, dtype=np.float64)
    if not trades:
        return pos
    ts_array = df_timestamps.astype(np.int64)
    for tr in trades:
        # Find index range from entry_time to exit_time (inclusive of entry, exclusive of exit)
        entry_idx = int(np.searchsorted(ts_array, tr.entry_time, side="left"))
        exit_idx = int(np.searchsorted(ts_array, tr.exit_time, side="left"))
        if entry_idx >= ts_array.size:
            continue
        sign = 1.0 if tr.side.lower() in ("long", "buy") else -1.0
        pos[entry_idx:exit_idx] = sign
    return pos


def _daily_vol_from_closes(closes: np.ndarray, periods_per_year: float) -> float:
    """Annualize-from-period: σ_daily = σ_period × √(periods_per_day).

    Returns the daily fractional volatility used by impact / capacity models.
    """
    closes = closes[closes > 0]
    if closes.size < 2:
        return float("nan")
    log_returns = np.diff(np.log(closes))
    period_vol = float(np.std(log_returns, ddof=1))
    periods_per_day = max(1.0, periods_per_year / 365.0)
    return float(period_vol * math.sqrt(periods_per_day))


def _adv_usd_from_df(df) -> float:
    """ADV in USD ≈ mean(volume × close) × periods_per_day.

    Uses the entire backtest window for stability; in live use this would be
    a recent rolling window, but for post-backtest validation the full-sample
    mean is the right reference quantity.
    """
    try:
        volume = df["volume"].to_numpy().astype(float)
        close = df["close"].to_numpy().astype(float)
    except Exception:
        return float("nan")
    notional_per_bar = volume * close
    notional_per_bar = notional_per_bar[np.isfinite(notional_per_bar)]
    if notional_per_bar.size == 0:
        return float("nan")
    # If we had T bars over a duration that's <T periods/day, scale up
    return float(np.mean(notional_per_bar) * 24.0)  # rough: assume 24 periods/day baseline


# ─── Phase implementations ──────────────────────────────────────────


def _phase_purged_cv(
    period_returns: np.ndarray, n_splits: int = 5, embargo_pct: float = 0.01
) -> dict:
    if period_returns.size < n_splits * 4:
        return {
            "status": "skipped",
            "reason": f"too few periods ({period_returns.size}) for {n_splits}-fold CV",
        }
    T = period_returns.size
    X = period_returns.reshape(-1, 1)  # any (T, 1) placeholder works for split shape
    t0 = np.arange(T, dtype=np.float64)
    t1 = t0 + 1  # point-in-time labels
    cv = PurgedKFold(n_splits=n_splits, embargo_pct=embargo_pct)
    fold_sharpes: list[float] = []
    for _, test_idx in cv.split(X, t1, t0):
        fold = period_returns[test_idx]
        if fold.size < 2 or float(np.std(fold)) == 0.0:
            fold_sharpes.append(0.0)
            continue
        sr = float(np.mean(fold) / np.std(fold))
        fold_sharpes.append(sr)
    return {
        "status": "ok",
        "n_splits": n_splits,
        "fold_sharpes_per_period": fold_sharpes,
        "mean_fold_sharpe": float(np.mean(fold_sharpes)) if fold_sharpes else 0.0,
        "n_positive_folds": int(sum(1 for s in fold_sharpes if s > 0)),
    }


def _phase_alpha_decay(
    position_series: np.ndarray, closes: np.ndarray, horizons: list[int]
) -> dict:
    if position_series.size < max(horizons) + 10:
        return {"status": "skipped", "reason": "insufficient bars for decay analysis"}
    if float(np.std(position_series)) == 0.0:
        return {"status": "skipped", "reason": "constant position (no signal variation)"}
    fwd_returns = make_forward_returns(closes, horizons)
    curve = compute_ic_curve(
        signal=position_series,
        forward_returns=fwd_returns,
        horizons=horizons,
        method="spearman",
        n_bootstrap=0,
    )
    fit = estimate_half_life(curve)
    return {
        "status": "ok",
        "horizons": list(map(int, curve.horizons.tolist())),
        "ics": [float(x) for x in curve.ics.tolist()],
        "half_life": float(fit.half_life) if np.isfinite(fit.half_life) else None,
        "tau": float(fit.tau) if np.isfinite(fit.tau) else None,
        "ic_initial": float(fit.ic_initial) if np.isfinite(fit.ic_initial) else None,
        "fit_r_squared": float(fit.r_squared) if np.isfinite(fit.r_squared) else None,
    }


def _phase_capacity(
    trades: list,
    adv_usd: float,
    daily_vol: float,
    max_impact_fraction: float,
    max_adv_pct: float,
) -> tuple[dict, Any]:
    """Returns (json_friendly_section, raw_CapacityResult_or_None)."""
    if not trades:
        return {"status": "skipped", "reason": "no trades to measure alpha from"}, None
    if not np.isfinite(adv_usd) or adv_usd <= 0:
        return {"status": "skipped", "reason": "ADV not available"}, None
    if not np.isfinite(daily_vol) or daily_vol <= 0:
        return {"status": "skipped", "reason": "daily vol not available"}, None

    # Alpha per trade in bps: mean of pnl_pct expressed in bps (pnl_pct is in %)
    pnl_pcts = np.array([t.pnl_pct for t in trades], dtype=np.float64)
    pnl_pcts = pnl_pcts[np.isfinite(pnl_pcts)]
    if pnl_pcts.size == 0:
        return {"status": "skipped", "reason": "no finite trade PnL"}, None
    alpha_bps = float(abs(np.mean(pnl_pcts)) * 100.0)  # 1% = 100 bps
    alpha_bps = max(alpha_bps, 1.0)  # floor for stability of the impact bisection

    result = analyze_capacity(
        alpha_bps=alpha_bps,
        impact_model=SqrtLawImpact(gamma=0.7),
        adv_usd=adv_usd,
        daily_vol=daily_vol,
        max_impact_fraction=max_impact_fraction,
        max_adv_pct=max_adv_pct,
    )
    json_section = {
        "status": "ok",
        "alpha_bps_measured": alpha_bps,
        "max_trade_size_usd": float(result.max_trade_size_usd),
        "binding_constraint": result.binding_constraint,
        "impact_constraint_usd": float(result.impact_constraint_usd),
        "adv_constraint_usd": float(result.adv_constraint_usd),
        "half_alpha_size_usd": float(result.half_alpha_size_usd),
        "breakeven_size_usd": float(result.breakeven_size_usd),
    }
    return json_section, result


def _phase_cross_impact(multi_pair_returns: dict) -> dict:
    """Cross-impact reporting for a multi-asset basket execution.

    `multi_pair_returns` is a dict mapping pair name → period returns array.
    Skips when fewer than 2 assets are available.
    """
    pairs = [p for p, r in multi_pair_returns.items() if r is not None and r.size > 10]
    if len(pairs) < 2:
        return {"status": "skipped", "reason": "fewer than 2 multi-pair series"}
    # Align lengths by truncating to the shortest series
    min_len = min(multi_pair_returns[p].size for p in pairs)
    R = np.column_stack([multi_pair_returns[p][:min_len] for p in pairs])
    rho = correlation_matrix(R)
    return {
        "status": "ok",
        "pairs": pairs,
        "correlation_matrix": [[float(x) for x in row] for row in rho.tolist()],
        "mean_off_diagonal_correlation": float(
            np.mean(rho[np.triu_indices_from(rho, k=1)])
        ),
        "note": (
            "Cross-impact matrix wired but no basket trade is simulated here. "
            "When the strategy executes as a basket, call basket_impact() with "
            "this correlation matrix + ADV/vol per asset to get the trade-level cost."
        ),
    }


def _phase_promotion(
    bt,
    period_returns: np.ndarray,
    cv_section: dict,
    capacity_result,
    gates_config: dict,
) -> dict:
    """Run promotion gates and return a JSON-friendly verdict dict.

    `capacity_result` is the raw substrate CapacityResult (or None if the
    capacity phase skipped). When None, the capacity gate is skipped too.
    """
    n_obs = int(period_returns.size)
    if n_obs < 2:
        return {"status": "skipped", "reason": "insufficient observations for gates"}

    per_period_sharpe = (
        float(np.mean(period_returns) / np.std(period_returns))
        if float(np.std(period_returns)) > 0
        else 0.0
    )

    gate_results = []

    # Gate 1: DSR
    gate_results.append(gate_deflated_sharpe(
        observed_sharpe=per_period_sharpe,
        n_observations=n_obs,
        n_trials=int(getattr(bt, "num_trials", 1) or 1),
        variance_of_trial_sharpes=0.0,
        min_dsr=float(gates_config["min_dsr"]),
    ))

    # Gate 2: CV pass rate (only if CV ran successfully)
    if cv_section.get("status") == "ok":
        gate_results.append(gate_cv_pass_rate(
            fold_sharpes=cv_section.get("fold_sharpes_per_period", []),
            min_sharpe_per_fold=float(gates_config["min_sharpe_per_fold"]),
            min_pass_rate=float(gates_config["min_cv_pass_rate"]),
        ))

    # Gate 3: capacity (only if capacity ran successfully)
    if capacity_result is not None:
        gate_results.append(gate_capacity(
            capacity_result=capacity_result,
            min_trade_size_usd=float(gates_config["min_capacity_usd"]),
        ))

    # Gate 4: track record
    gate_results.append(gate_track_record(
        n_observations=n_obs,
        n_trades=int(bt.num_trades),
        min_observations=int(gates_config["min_observations"]),
        min_trades=int(gates_config["min_trades"]),
    ))

    # Gate 5: max drawdown
    gate_results.append(gate_max_drawdown(
        returns=period_returns,
        max_dd_pct=float(gates_config["max_dd_pct"]),
    ))

    verdict = evaluate_promotion(gate_results)
    return {
        "status": "ok",
        "passed": bool(verdict.overall_passed),
        "n_gates": len(gate_results),
        "n_passed": sum(1 for g in gate_results if g.passed),
        "gates": [
            {
                "name": g.name,
                "passed": bool(g.passed),
                "metric_value": float(g.metric_value),
                "threshold": float(g.threshold),
                "comparison": g.comparison,
                "details": g.details,
            }
            for g in gate_results
        ],
        "failures": [g.name for g in verdict.failures()],
    }


def _phase_seal_bundle(
    strategy_name: str,
    pair: str,
    interval: str,
    config_overrides: dict | None,
    result_sections: dict,
    seed: int | None,
    bundles_dir: str | None = None,
) -> dict:
    """Hash + write a SealedBundle for this research run."""
    config = {
        "strategy_name": strategy_name,
        "pair": pair,
        "interval": interval,
        "config_overrides": config_overrides or {},
    }
    bundle = SealedBundle.from_inputs(
        bundle_type="custom",
        data_hash=hash_text(f"research_pipeline:{strategy_name}:{pair}:{interval}"),
        config_hash=hash_canonical_json(config),
        result_hash=hash_canonical_json(result_sections),
        code_hash="",
        rng_seed=seed,
        metadata={
            "strategy_name": strategy_name,
            "pair": pair,
            "interval": interval,
            "source": "rift_research.advanced.run_advanced_validations",
        },
    )
    from pathlib import Path
    out_dir = Path(bundles_dir) if bundles_dir else None
    path = save_bundle(bundle, bundles_dir=out_dir)
    return {
        "status": "ok",
        "bundle_id": bundle.bundle_id,
        "path": str(path),
    }


# ─── Public entry point ─────────────────────────────────────────────


def run_advanced_validations(
    bt,
    df,
    strategy_cls,
    strategy_name: str,
    pair: str,
    interval: str,
    multi_pair_results: list[dict] | None = None,
    config_overrides: dict | None = None,
    seed: int | None = None,
    emit_fn=None,
    bundles_dir: str | None = None,
) -> dict:
    """Run all advanced validations on a completed backtest.

    Returns a dict with keys:
      purged_cv, alpha_decay, capacity, cross_impact, promotion_verdict,
      sealed_bundle.

    Each value is a JSON-friendly dict with at least `status` ∈ {"ok",
    "skipped", "error"}.

    `emit_fn` optionally receives per-section progress events compatible
    with the research-pipeline's JSON streaming protocol. Signature:
      emit_fn({"type": "step", "step": int, "name": str, "msg": str})
    """
    def _emit(payload: dict) -> None:
        if emit_fn is not None:
            try:
                emit_fn(payload)
            except Exception:
                pass

    # Promotion gate config from strategy with sensible defaults
    gates_config = dict(_DEFAULT_GATES)
    user_gates = getattr(strategy_cls, "promotion_gates", None) or {}
    if isinstance(user_gates, dict):
        gates_config.update(user_gates)

    # Derive period returns + position series + market stats
    period_returns = _period_returns_from_equity_curve(bt.equity_curve)
    try:
        timestamps = df["timestamp"].to_numpy().astype(np.int64)
    except Exception:
        timestamps = np.arange(len(df), dtype=np.int64)
    position_series = _position_series_from_trades(bt.trades, timestamps)
    try:
        closes = df["close"].to_numpy().astype(float)
    except Exception:
        closes = np.array([], dtype=np.float64)
    periods_per_year = periods_per_year_for_interval(interval)
    daily_vol = _daily_vol_from_closes(closes, periods_per_year)
    adv_usd = _adv_usd_from_df(df)

    # ─── Purged CV ───────────────────────────────────────────────
    _emit({"type": "step", "step": 9, "name": "purged_cv", "msg": "Running purged k-fold cross-validation..."})
    try:
        cv_section = _phase_purged_cv(period_returns)
    except Exception as exc:
        cv_section = {"status": "error", "error": str(exc)}
    _emit({"type": "step_done", "step": 9, "msg": f"Purged CV: {cv_section.get('status')}"})

    # ─── Alpha decay ─────────────────────────────────────────────
    _emit({"type": "step", "step": 10, "name": "alpha_decay", "msg": "Estimating alpha decay / half-life..."})
    try:
        decay_section = _phase_alpha_decay(
            position_series=position_series,
            closes=closes,
            horizons=[1, 2, 5, 10, 20, 50],
        )
    except Exception as exc:
        decay_section = {"status": "error", "error": str(exc)}
    _emit({"type": "step_done", "step": 10, "msg": f"Alpha decay: {decay_section.get('status')}"})

    # ─── Capacity ────────────────────────────────────────────────
    _emit({"type": "step", "step": 11, "name": "capacity", "msg": "Computing capacity (impact + ADV)..."})
    capacity_result_obj = None
    try:
        capacity_section, capacity_result_obj = _phase_capacity(
            trades=bt.trades,
            adv_usd=adv_usd,
            daily_vol=daily_vol,
            max_impact_fraction=float(gates_config["max_impact_fraction"]),
            max_adv_pct=float(gates_config["max_adv_pct"]),
        )
    except Exception as exc:
        capacity_section = {"status": "error", "error": str(exc)}
    _emit({"type": "step_done", "step": 11, "msg": f"Capacity: {capacity_section.get('status')}"})

    # ─── Cross-impact ────────────────────────────────────────────
    _emit({"type": "step", "step": 12, "name": "cross_impact", "msg": "Computing cross-impact (multi-asset)..."})
    cross_impact_section: dict
    if multi_pair_results and len(multi_pair_results) >= 1:
        # We only have returns aggregates per pair from the runner — not the
        # full series. So we report a "n/a — single-asset run" status unless
        # we have full per-bar return series available. The runner already
        # discards intermediate series; cross-impact reporting at the basket
        # level needs to be enabled by a future runner change.
        cross_impact_section = {
            "status": "skipped",
            "reason": "multi-pair returns series not exposed by current runner",
        }
    else:
        cross_impact_section = {
            "status": "skipped",
            "reason": "single-asset run — cross-impact not applicable",
        }
    _emit({"type": "step_done", "step": 12, "msg": f"Cross-impact: {cross_impact_section.get('status')}"})

    # ─── Promotion verdict ───────────────────────────────────────
    _emit({"type": "step", "step": 13, "name": "promotion", "msg": "Evaluating promotion gates..."})
    try:
        promotion_section = _phase_promotion(
            bt=bt,
            period_returns=period_returns,
            cv_section=cv_section,
            capacity_result=capacity_result_obj,
            gates_config=gates_config,
        )
    except Exception as exc:
        promotion_section = {"status": "error", "error": str(exc)}
    if promotion_section.get("status") == "ok":
        verdict_label = "PASS" if promotion_section["passed"] else "FAIL"
        n_pass = promotion_section["n_passed"]
        n_total = promotion_section["n_gates"]
        _emit({"type": "step_done", "step": 13, "msg": f"Promotion: {verdict_label} ({n_pass}/{n_total} gates)"})
    else:
        _emit({"type": "step_done", "step": 13, "msg": f"Promotion: {promotion_section.get('status')}"})

    # ─── Sealed bundle ───────────────────────────────────────────
    _emit({"type": "step", "step": 14, "name": "bundle", "msg": "Sealing reproducibility bundle..."})
    aggregated = {
        "backtest": {
            "total_return_pct": float(bt.total_return_pct),
            "sharpe_ratio": float(bt.sharpe_ratio),
            "max_drawdown_pct": float(bt.max_drawdown_pct),
            "num_trades": int(bt.num_trades),
        },
        "purged_cv": cv_section,
        "alpha_decay": decay_section,
        "capacity": capacity_section,
        "cross_impact": cross_impact_section,
        "promotion_verdict": promotion_section,
    }
    try:
        bundle_section = _phase_seal_bundle(
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            config_overrides=config_overrides,
            result_sections=aggregated,
            seed=seed,
            bundles_dir=bundles_dir,
        )
    except Exception as exc:
        bundle_section = {"status": "error", "error": str(exc)}
    _emit({"type": "step_done", "step": 14, "msg": f"Bundle: {bundle_section.get('status')}"})

    return {
        "purged_cv": cv_section,
        "alpha_decay": decay_section,
        "capacity": capacity_section,
        "cross_impact": cross_impact_section,
        "promotion_verdict": promotion_section,
        "sealed_bundle": bundle_section,
    }
