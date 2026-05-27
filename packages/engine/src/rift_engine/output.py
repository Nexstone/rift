"""Beautiful output formatting for backtest results."""

from __future__ import annotations

import json
from rift_engine.backtest import BacktestResult


def generate_ascii_chart(equity_curve: list[float], width: int = 60, height: int = 15) -> list[str]:
    """Generate an ASCII equity curve chart."""
    if len(equity_curve) < 2:
        return ["  (not enough data for chart)"]

    # Downsample to fit width
    step = max(1, len(equity_curve) // width)
    sampled = [equity_curve[i] for i in range(0, len(equity_curve), step)]
    if len(sampled) > width:
        sampled = sampled[:width]

    min_val = min(sampled)
    max_val = max(sampled)
    val_range = max_val - min_val

    if val_range == 0:
        val_range = 1

    lines = []

    # Chart body
    for row in range(height, -1, -1):
        threshold = min_val + (val_range * row / height)

        # Y-axis label
        if row == height:
            label = f"${max_val:>10,.0f} "
        elif row == 0:
            label = f"${min_val:>10,.0f} "
        elif row == height // 2:
            mid = (max_val + min_val) / 2
            label = f"${mid:>10,.0f} "
        else:
            label = "             "

        line = label + "│"
        for val in sampled:
            if val >= threshold:
                line += "█"
            else:
                line += " "
        lines.append(line)

    # X-axis
    lines.append("             └" + "─" * len(sampled))

    return lines


def format_result_full(result: BacktestResult) -> dict:
    """Format a full backtest result with chart and metrics for NDJSON output."""
    chart_lines = generate_ascii_chart(result.equity_curve)

    # Trade distribution
    wins = sum(1 for t in result.trades if t.pnl > 0)
    losses = sum(1 for t in result.trades if t.pnl <= 0)

    # Best/worst trades
    best_trade = max((t.pnl_pct for t in result.trades), default=0)
    worst_trade = min((t.pnl_pct for t in result.trades), default=0)

    # Avg trade duration (in candles — approximate)
    avg_duration = 0
    if result.trades:
        durations = [t.exit_time - t.entry_time for t in result.trades]
        avg_duration = sum(durations) / len(durations)

    # Consecutive wins/losses
    max_consec_wins = 0
    max_consec_losses = 0
    current_wins = 0
    current_losses = 0
    for t in result.trades:
        if t.pnl > 0:
            current_wins += 1
            current_losses = 0
            max_consec_wins = max(max_consec_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_consec_losses = max(max_consec_losses, current_losses)

    return {
        "type": "result",
        "command": "backtest",
        **result.to_dict(),
        "chart": chart_lines,
        "wins": wins,
        "losses": losses,
        "best_trade_pct": round(best_trade, 2),
        "worst_trade_pct": round(worst_trade, 2),
        "max_consec_wins": max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "avg_duration_ms": round(avg_duration),
        "export": result.to_export_dict(),
    }
