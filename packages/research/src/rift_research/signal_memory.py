"""Signal memory — learns which signal combinations actually work.

Stores signal → outcome pairs from every closed trade and computes
hit rates. Used by Scout to boost signals that historically work
and penalize signals that historically fail.

Storage: ~/.rift/signal_memory.jsonl (append-only NDJSON)
No LLM. No AI. Just a growing lookup table of what actually works.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path


MEMORY_FILE = Path.home() / ".rift" / "signal_memory.jsonl"


def record_outcome(
    coin: str,
    direction: str,
    signals: list[str],
    pnl_pct: float,
    source: str = "live",
    hold_minutes: float = 0.0,
    time_to_peak_minutes: float = 0.0,
) -> None:
    """Record a trade outcome for signal learning.

    Called when a trade closes — from live.py, manual_trade.py, recon, or backfill.

    Args:
        source: "live", "sim", "recon", "recon_sim", "backfill", "manual"
        hold_minutes: How long the position was held
        time_to_peak_minutes: When max favorable excursion was reached (from entry)
    """
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "coin": coin.upper(),
        "direction": direction.lower(),
        "signals": sorted(signals),
        "result": "win" if pnl_pct > 0 else "loss",
        "pnl_pct": round(pnl_pct, 2),
        "source": source,
        "hold_minutes": round(hold_minutes, 1),
        "time_to_peak_minutes": round(time_to_peak_minutes, 1),
        "timestamp": time.time(),
    }

    with open(MEMORY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def get_hit_rate(
    coin: str,
    direction: str,
    signals: list[str],
    min_observations: int = 5,
) -> float | None:
    """Get the historical hit rate for a signal combination.

    Tries combination-level first (exact match). Falls back to
    average of individual signal hit rates if not enough combo data.

    Returns hit rate (0.0-1.0) or None if insufficient data.
    """
    records = _load_records()
    if not records:
        return None

    coin = coin.upper()
    direction = direction.lower()
    sorted_signals = sorted(signals)

    # Level 1: Exact combination match (coin + direction + signals)
    combo_key = f"{coin}:{direction}:{','.join(sorted_signals)}"
    combo_wins = 0
    combo_total = 0

    for r in records:
        if r["coin"] == coin and r["direction"] == direction and sorted(r["signals"]) == sorted_signals:
            combo_total += 1
            if r["result"] == "win":
                combo_wins += 1

    if combo_total >= min_observations:
        return combo_wins / combo_total

    # Level 2: Average individual signal hit rates (any coin, same direction)
    signal_rates: list[float] = []
    for sig in signals:
        wins = 0
        total = 0
        for r in records:
            if r["direction"] == direction and sig in r["signals"]:
                total += 1
                if r["result"] == "win":
                    wins += 1
        if total >= min_observations:
            signal_rates.append(wins / total)

    if signal_rates:
        return sum(signal_rates) / len(signal_rates)

    # Level 3: Direction-level hit rate for this coin
    dir_wins = 0
    dir_total = 0
    for r in records:
        if r["coin"] == coin and r["direction"] == direction:
            dir_total += 1
            if r["result"] == "win":
                dir_wins += 1

    if dir_total >= min_observations:
        return dir_wins / dir_total

    return None


def get_signal_stats(min_observations: int = 3) -> dict:
    """Get hit rate stats for all signals — for debugging and reports.

    Returns dict of {signal_name: {wins, total, hit_rate}} sorted by total.
    """
    records = _load_records()
    if not records:
        return {}

    stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "total": 0})

    for r in records:
        for sig in r["signals"]:
            key = f"{sig}:{r['direction']}"
            stats[key]["total"] += 1
            if r["result"] == "win":
                stats[key]["wins"] += 1

    result = {}
    for key, s in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True):
        if s["total"] >= min_observations:
            result[key] = {
                "wins": s["wins"],
                "total": s["total"],
                "hit_rate": round(s["wins"] / s["total"], 3),
            }

    return result


def get_kelly_sizing(
    coin: str,
    direction: str,
    signals: list[str],
    kelly_fraction: float = 0.5,
    min_observations: int = 10,
) -> dict | None:
    """Compute Kelly-based position sizing from signal memory records.

    Uses the same Kelly formula as strategy.py::compute_kelly_risk() but
    operates on signal memory records instead of Trade objects.

    Returns dict with risk_pct, win_rate, avg_win, avg_loss, observations
    or None if insufficient data.
    """
    records = _load_records()
    if not records:
        return None

    direction = direction.lower()

    # Find records where any signal overlaps with the provided list
    matching = []
    signal_set = set(signals)
    for r in records:
        if r["direction"] != direction:
            continue
        if signal_set & set(r["signals"]):  # any overlap
            matching.append(r)

    if len(matching) < min_observations:
        return None

    wins = [r for r in matching if r["result"] == "win"]
    losses = [r for r in matching if r["result"] == "loss"]

    if not wins or not losses:
        return None

    win_rate = len(wins) / len(matching)
    avg_win = sum(abs(r["pnl_pct"]) for r in wins) / len(wins)
    avg_loss = sum(abs(r["pnl_pct"]) for r in losses) / len(losses)

    if avg_loss == 0 or avg_win == 0:
        return None

    # Kelly formula: f* = (p * b - q) / b
    # p = win probability, q = loss probability, b = avg_win / avg_loss
    b = avg_win / avg_loss
    kelly = (win_rate * b - (1 - win_rate)) / b

    # Apply fractional Kelly and clamp
    risk_pct = kelly * kelly_fraction
    risk_pct = max(0.005, min(risk_pct, 0.05))  # floor 0.5%, cap 5%

    return {
        "risk_pct": round(risk_pct, 4),
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "observations": len(matching),
    }


def get_signal_decay(signal_name: str, min_observations: int = 5) -> dict | None:
    """Get timing stats for a signal — how fast the predicted move peaks.

    Returns avg/median/p90 time-to-peak in minutes, or None if insufficient data.
    """
    records = _load_records()
    times = []
    holds = []

    for r in records:
        if signal_name in r.get("signals", []):
            ttp = r.get("time_to_peak_minutes", 0)
            if ttp > 0:
                times.append(ttp)
            hm = r.get("hold_minutes", 0)
            if hm > 0:
                holds.append(hm)

    if len(times) < min_observations:
        return None

    times.sort()
    median = times[len(times) // 2]
    p90_idx = min(len(times) - 1, int(len(times) * 0.9))

    return {
        "avg_time_to_peak_min": round(sum(times) / len(times), 1),
        "median": round(median, 1),
        "p90": round(times[p90_idx], 1),
        "avg_hold_min": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "observations": len(times),
    }


def get_memory_size() -> int:
    """Get total number of recorded outcomes."""
    if not MEMORY_FILE.exists():
        return 0
    try:
        return sum(1 for line in MEMORY_FILE.read_text().strip().split("\n") if line.strip())
    except Exception:
        return 0


def _load_records() -> list[dict]:
    """Load all records from the memory file."""
    if not MEMORY_FILE.exists():
        return []
    records = []
    for line in MEMORY_FILE.read_text().strip().split("\n"):
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records
