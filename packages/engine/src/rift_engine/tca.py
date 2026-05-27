"""Transaction Cost Analysis (TCA) for RIFT live trading.

Analyzes execution quality by comparing fill prices to mid prices
at the time of order submission. Grades execution on a 0-100 scale
relative to the asset's ATR (volatility-normalized).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path


BUILDER_FEE_RATE = 0.0003  # 0.03% per side


@dataclass
class TCAReport:
    """Transaction cost analysis results."""
    # Per-trade breakdown
    trades: list[dict] = field(default_factory=list)

    # Aggregate slippage
    avg_entry_slippage_bps: float = 0.0
    avg_exit_slippage_bps: float = 0.0
    total_slippage_bps: float = 0.0      # weighted average across all fills

    # Dollar costs
    total_slippage_cost: float = 0.0     # $ lost to slippage
    total_fee_cost: float = 0.0          # $ paid in builder fees
    total_execution_cost: float = 0.0    # slippage + fees

    # Execution method comparison
    ioc_avg_slippage_bps: float = 0.0
    twap_avg_slippage_bps: float = 0.0
    ioc_count: int = 0
    twap_count: int = 0

    # Market impact
    avg_market_impact_bps: float = 0.0

    # Latency
    avg_signal_to_fill_ms: float = 0.0
    avg_submit_to_fill_ms: float = 0.0
    p99_latency_ms: float = 0.0

    # Post-trade markouts — mean across all entries, in bps
    # (positive = trader's edge; price moved in trader's favor after entry)
    markouts_bps: dict[str, float] = field(default_factory=dict)  # {"t+1s": ..., "t+10s": ..., ...}
    markout_horizons_seconds: list[int] = field(default_factory=list)
    markout_n_fills: int = 0  # how many fills contributed (some may lack candle data)

    # Grade
    score: int = 0      # 0-100
    grade: str = "N/A"  # A-F


def analyze_trades(trades: list[dict], atr_bps: float = 0.0) -> TCAReport:
    """Run TCA on a list of trade dicts (from session logs).

    Args:
        trades: List of trade dicts with TCA fields
        atr_bps: Asset's average true range in basis points (for grading).
                 If 0, grade is based on absolute slippage thresholds.
    """
    report = TCAReport()

    if not trades:
        report.grade = "N/A"
        return report

    entry_slippages: list[float] = []
    exit_slippages: list[float] = []
    total_notional = 0.0
    total_slip_dollars = 0.0
    total_fee_dollars = 0.0
    ioc_slips: list[float] = []
    twap_slips: list[float] = []

    for t in trades:
        entry_price = t.get("entry_price", 0)
        exit_price = t.get("exit_price", 0)
        size = t.get("size", 0)
        entry_mid = t.get("entry_mid_price", 0)
        exit_mid = t.get("exit_mid_price", 0)
        entry_slip = t.get("entry_slippage_bps", 0)
        exit_slip = t.get("exit_slippage_bps", 0)
        method = t.get("execution_method", "ioc")
        side = t.get("side", "long")

        # Recompute slippage if not provided but mid prices are
        if entry_slip == 0 and entry_mid > 0 and entry_price > 0:
            raw = (entry_price - entry_mid) / entry_mid * 10000
            entry_slip = raw if side == "long" else -raw

        if exit_slip == 0 and exit_mid > 0 and exit_price > 0:
            raw = (exit_price - exit_mid) / exit_mid * 10000
            exit_slip = -raw if side == "long" else raw

        entry_notional = size * entry_price
        exit_notional = size * exit_price
        trade_notional = entry_notional + exit_notional
        total_notional += trade_notional

        # Dollar cost of slippage
        entry_slip_dollars = abs(entry_slip / 10000) * entry_notional
        exit_slip_dollars = abs(exit_slip / 10000) * exit_notional
        total_slip_dollars += entry_slip_dollars + exit_slip_dollars

        # Fee cost
        fee = trade_notional * BUILDER_FEE_RATE
        total_fee_dollars += fee

        entry_slippages.append(abs(entry_slip))
        exit_slippages.append(abs(exit_slip))

        if method == "twap":
            twap_slips.append(abs(entry_slip))
            report.twap_count += 1
        else:
            ioc_slips.append(abs(entry_slip))
            report.ioc_count += 1

        # Per-trade record
        report.trades.append({
            "side": side,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "entry_mid": round(entry_mid, 2) if entry_mid else None,
            "exit_mid": round(exit_mid, 2) if exit_mid else None,
            "entry_slippage_bps": round(entry_slip, 2),
            "exit_slippage_bps": round(exit_slip, 2),
            "slippage_cost": round(entry_slip_dollars + exit_slip_dollars, 2),
            "fee_cost": round(fee, 2),
            "total_cost": round(entry_slip_dollars + exit_slip_dollars + fee, 2),
            "execution_method": method,
            "pnl": round(t.get("pnl", 0), 2),
        })

    # Aggregates
    report.avg_entry_slippage_bps = round(_safe_mean(entry_slippages), 2)
    report.avg_exit_slippage_bps = round(_safe_mean(exit_slippages), 2)
    all_slips = entry_slippages + exit_slippages
    report.total_slippage_bps = round(_safe_mean(all_slips), 2)

    report.total_slippage_cost = round(total_slip_dollars, 2)
    report.total_fee_cost = round(total_fee_dollars, 2)
    report.total_execution_cost = round(total_slip_dollars + total_fee_dollars, 2)

    # Market impact (average slippage weighted by notional — simplified)
    report.avg_market_impact_bps = report.total_slippage_bps

    # Method comparison
    report.ioc_avg_slippage_bps = round(_safe_mean(ioc_slips), 2)
    report.twap_avg_slippage_bps = round(_safe_mean(twap_slips), 2)

    # Latency analysis
    latencies_total: list[float] = []
    latencies_submit: list[float] = []
    for t in trades:
        sig_ts = t.get("signal_ts", 0)
        sub_ts = t.get("submit_ts", 0)
        fil_ts = t.get("fill_ts", 0)
        if sig_ts > 0 and fil_ts > 0:
            latencies_total.append((fil_ts - sig_ts) * 1000)
        if sub_ts > 0 and fil_ts > 0:
            latencies_submit.append((fil_ts - sub_ts) * 1000)
    report.avg_signal_to_fill_ms = round(_safe_mean(latencies_total), 1)
    report.avg_submit_to_fill_ms = round(_safe_mean(latencies_submit), 1)
    if latencies_total:
        sorted_lat = sorted(latencies_total)
        p99_idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.99))
        report.p99_latency_ms = round(sorted_lat[p99_idx], 1)

    # Grade
    report.score, report.grade = _compute_grade(report.total_slippage_bps, atr_bps)

    return report


def compute_session_markouts(
    trades: list[dict],
    pair: str,
    interval: str = "1m",
    horizons_seconds: list[int] | None = None,
    data_dir=None,
) -> dict:
    """Compute aggregate post-trade markouts from session trades + cached candles.

    For each entry fill in the session, looks up the price at t+Ns horizons
    from the cached candle data (highest available resolution — default 1m
    if synced, falls back to lower-frequency if not).

    Sign convention (from substrate.frictions.markouts): positive markout_bps
    means the price moved in the trader's favor after entry (good fill);
    negative means adverse selection (price went against them post-fill).

    Args:
      trades:           list of trade dicts (each needs entry_ts + entry_price + side)
      pair:             coin name for the candle lookup
      interval:         candle interval to use as the post-fill price series
      horizons_seconds: optional horizons override (default [1, 10, 60, 300])
      data_dir:         optional override for the candle data directory
                        (used by tests; default = the rift_data DEFAULT_DATA_DIR)

    Returns dict suitable for embedding into TCAReport.markouts_bps.
    """
    from rift_data.data import DEFAULT_DATA_DIR, load_candles
    from rift_core.schema import normalize_coin
    from rift_substrate.frictions.markouts import (
        DEFAULT_HORIZONS_SECONDS,
        compute_markouts,
    )

    horizons = horizons_seconds if horizons_seconds else DEFAULT_HORIZONS_SECONDS
    empty = {
        "horizons_seconds": list(horizons),
        "markouts_bps": {f"t+{h}s": 0.0 for h in horizons},
        "n_fills": 0,
    }

    if not trades:
        return empty

    coin = normalize_coin(pair)
    df = load_candles(coin, interval, data_dir if data_dir is not None else DEFAULT_DATA_DIR)
    if df is None or len(df) == 0:
        return empty

    # Build the post-fill price series (timestamps + closes)
    timestamps = df["timestamp"].to_numpy()  # epoch ms
    closes = df["close"].to_numpy().astype(float)

    # Per-horizon accumulators
    per_horizon: dict[int, list[float]] = {h: [] for h in horizons}

    for t in trades:
        entry_ts = int(t.get("entry_ts", 0) or t.get("entry_time", 0))
        entry_price = float(t.get("entry_price", 0))
        side = t.get("side", "long")
        if entry_ts <= 0 or entry_price <= 0:
            continue
        # Slice timestamps + prices AFTER the entry
        mask = timestamps > entry_ts
        if not mask.any():
            continue
        post_ts = timestamps[mask]
        post_px = closes[mask]

        ms = compute_markouts(
            fill_price=entry_price,
            fill_timestamp_ms=entry_ts,
            side="long" if side == "long" else "short",
            subsequent_timestamps_ms=post_ts,
            subsequent_prices=post_px,
            horizons_seconds=horizons,
        )
        for h, m in zip(ms.horizons_seconds, ms.markouts_bps):
            if m == m:  # not NaN
                per_horizon[h].append(m)

    # Aggregate: mean per horizon
    means: dict[str, float] = {}
    n_fills = max((len(v) for v in per_horizon.values()), default=0)
    for h in horizons:
        vals = per_horizon[h]
        means[f"t+{h}s"] = round(sum(vals) / len(vals), 3) if vals else 0.0

    return {
        "horizons_seconds": list(horizons),
        "markouts_bps": means,
        "n_fills": n_fills,
    }


def _safe_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _compute_grade(avg_slippage_bps: float, atr_bps: float) -> tuple[int, str]:
    """Grade execution quality.

    If ATR is provided, grades relative to volatility:
      <5% of ATR = A, <10% = B, <15% = C, <20% = D, else F

    If no ATR, uses absolute thresholds:
      <2bps = A, <5bps = B, <10bps = C, <20bps = D, else F
    """
    if atr_bps > 0:
        ratio = avg_slippage_bps / atr_bps
        if ratio < 0.05:
            return 95, "A"
        elif ratio < 0.10:
            return 80, "B"
        elif ratio < 0.15:
            return 65, "C"
        elif ratio < 0.20:
            return 45, "D"
        else:
            return 25, "F"
    else:
        if avg_slippage_bps < 2:
            return 95, "A"
        elif avg_slippage_bps < 5:
            return 80, "B"
        elif avg_slippage_bps < 10:
            return 65, "C"
        elif avg_slippage_bps < 20:
            return 45, "D"
        else:
            return 25, "F"


def analyze_session_log(log_path: str, data_dir=None) -> TCAReport:
    """Run TCA from a saved session log file.

    If the session JSON includes `pair` + `interval` metadata, also computes
    post-fill markouts at standard horizons (t+1s/10s/60s/300s) from the
    cached candle data and embeds them into the report.

    `data_dir` overrides the candle cache location (used by tests).
    """
    path = Path(log_path)
    if not path.exists():
        return TCAReport()
    data = json.loads(path.read_text())
    trades = data.get("trades", [])
    pair = data.get("pair") or data.get("summary", {}).get("pair")
    interval = data.get("interval", "1m")

    report = analyze_trades(trades)
    if pair and trades:
        try:
            mk = compute_session_markouts(
                trades, pair=pair, interval=interval, data_dir=data_dir,
            )
            report.markouts_bps = mk["markouts_bps"]
            report.markout_horizons_seconds = mk["horizons_seconds"]
            report.markout_n_fills = mk["n_fills"]
        except Exception:
            # Markouts are advisory — never let their failure kill the TCA path
            pass
    return report


def analyze_all_sessions(sessions_dir: str = "") -> TCAReport:
    """Run TCA across all saved session logs.

    Markouts are aggregated per-pair when computing, then averaged across
    pairs weighted by fill count. Sessions without pair metadata contribute
    to slippage/fee stats but not markouts.
    """
    d = Path(sessions_dir) if sessions_dir else Path.home() / ".rift" / "algo_sessions"
    if not d.exists():
        return TCAReport()
    all_trades: list[dict] = []
    # Group trades by (pair, interval) so we can compute markouts efficiently
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for f in sorted(d.glob("LIVE_*.json")):
        try:
            data = json.loads(f.read_text())
            session_trades = data.get("trades", [])
            all_trades.extend(session_trades)
            pair = data.get("pair") or data.get("summary", {}).get("pair")
            interval = data.get("interval", "1m")
            if pair:
                by_pair.setdefault((pair, interval), []).extend(session_trades)
        except Exception:
            pass

    report = analyze_trades(all_trades)

    # Aggregate markouts across all pairs
    from collections import defaultdict
    horizon_totals: dict[int, float] = defaultdict(float)
    horizon_counts: dict[int, int] = defaultdict(int)
    horizons_used: list[int] = []
    total_fills = 0
    for (pair, interval), trades in by_pair.items():
        try:
            mk = compute_session_markouts(trades, pair=pair, interval=interval)
            horizons_used = mk["horizons_seconds"]
            for h_key, val in mk["markouts_bps"].items():
                # h_key like "t+1s"
                h = int(h_key.lstrip("t+").rstrip("s"))
                if mk["n_fills"] > 0:
                    horizon_totals[h] += val * mk["n_fills"]
                    horizon_counts[h] += mk["n_fills"]
            total_fills += mk["n_fills"]
        except Exception:
            pass

    if horizons_used:
        report.markouts_bps = {
            f"t+{h}s": round(horizon_totals[h] / horizon_counts[h], 3) if horizon_counts[h] > 0 else 0.0
            for h in horizons_used
        }
        report.markout_horizons_seconds = horizons_used
        report.markout_n_fills = total_fills

    return report
