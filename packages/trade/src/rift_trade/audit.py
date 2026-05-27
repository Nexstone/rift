"""Compliance audit trail export for RIFT.

Produces structured CSV or JSON trade logs suitable for compliance
teams, prime brokers, and regulatory reporting. Each trade becomes
two rows (OPEN + CLOSE) linked by trade_id.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path


EXPORT_DIR = Path.home() / ".rift" / "reports"


def export_audit_trail(
    output_format: str = "csv",
    last_days: int = 30,
    strategy: str = "",
    output_path: str = "",
) -> str:
    """Export compliance-grade trade log.

    Args:
        output_format: "csv" or "json"
        last_days: How many days of history to include
        strategy: Filter by strategy name (empty = all)
        output_path: Custom output path (empty = default)

    Returns path to the exported file.
    """
    sessions_dir = Path.home() / ".rift" / "algo_sessions"
    if not sessions_dir.exists():
        return ""

    cutoff = datetime.now() - timedelta(days=last_days)
    rows: list[dict] = []

    for log_file in sorted(sessions_dir.glob("ALGO_*.json")):
        try:
            data = json.loads(log_file.read_text())
        except Exception:
            continue

        # Filter by strategy
        strat_name = data.get("strategy", "")
        if strategy and strat_name != strategy:
            continue

        # Filter by date
        started_at = data.get("started_at", "")
        try:
            session_date = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            if session_date < cutoff:
                continue
        except (ValueError, TypeError):
            pass

        wallet = data.get("wallet", "")
        pair = data.get("pair", "")
        session_id = log_file.stem
        initial_equity = data.get("initial_equity", 0)

        for trade in data.get("trades", []):
            entry_price = trade.get("entry_price", 0)
            exit_price = trade.get("exit_price", 0)
            size = trade.get("size", 0)
            side = trade.get("side", "").upper()
            oid = trade.get("oid", "")
            entry_time = trade.get("entry_time", "")
            exit_time = trade.get("exit_time", "")
            entry_mid = trade.get("entry_mid_price", 0)
            exit_mid = trade.get("exit_mid_price", 0)
            entry_slip = trade.get("entry_slippage_bps", 0)
            exit_slip = trade.get("exit_slippage_bps", 0)
            method = trade.get("execution_method", "ioc")
            pnl = trade.get("pnl", 0)
            funding = trade.get("funding", trade.get("funding_collected", 0))
            exit_reason = trade.get("exit_reason", "")
            signal_ts = trade.get("signal_ts", 0)
            submit_ts = trade.get("submit_ts", 0)
            fill_ts = trade.get("fill_ts", 0)

            # Reconstruct full timestamps
            entry_ts_str = _reconstruct_timestamp(started_at, entry_time)
            exit_ts_str = _reconstruct_timestamp(started_at, exit_time)

            entry_notional = size * entry_price
            exit_notional = size * exit_price
            entry_fee = entry_notional * 0.001
            exit_fee = exit_notional * 0.001

            # Latency
            latency_ms = round((fill_ts - signal_ts) * 1000, 1) if signal_ts > 0 and fill_ts > 0 else None

            # OPEN row
            rows.append({
                "timestamp_utc": entry_ts_str,
                "trade_id": oid or "",
                "strategy": strat_name,
                "pair": pair,
                "side": side,
                "action": "OPEN",
                "size": round(size, 6),
                "price": round(entry_price, 2),
                "mid_price": round(entry_mid, 2) if entry_mid else "",
                "slippage_bps": round(entry_slip, 2),
                "fee_usd": round(entry_fee, 2),
                "notional_usd": round(entry_notional, 2),
                "pnl_usd": "",
                "funding_usd": "",
                "exit_reason": "",
                "execution_method": method,
                "latency_ms": latency_ms or "",
                "wallet": wallet,
                "exchange": "Hyperliquid",
                "session_id": session_id,
            })

            # CLOSE row
            rows.append({
                "timestamp_utc": exit_ts_str,
                "trade_id": oid or "",
                "strategy": strat_name,
                "pair": pair,
                "side": side,
                "action": "CLOSE",
                "size": round(size, 6),
                "price": round(exit_price, 2),
                "mid_price": round(exit_mid, 2) if exit_mid else "",
                "slippage_bps": round(exit_slip, 2),
                "fee_usd": round(exit_fee, 2),
                "notional_usd": round(exit_notional, 2),
                "pnl_usd": round(pnl, 2),
                "funding_usd": round(funding, 2),
                "exit_reason": exit_reason,
                "execution_method": method,
                "latency_ms": "",
                "wallet": wallet,
                "exchange": "Hyperliquid",
                "session_id": session_id,
            })

    if not rows:
        return ""

    # Output
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d")
        ext = "csv" if output_format == "csv" else "json"
        output_path = str(EXPORT_DIR / f"audit_{timestamp}.{ext}")

    if output_format == "csv":
        _write_csv(rows, output_path)
    else:
        _write_json(rows, output_path)

    return output_path


def _reconstruct_timestamp(session_start: str, trade_time: str) -> str:
    """Reconstruct full ISO timestamp from session date + trade HH:MM."""
    if not session_start or not trade_time:
        return ""
    try:
        session_dt = datetime.strptime(session_start, "%Y-%m-%d %H:%M:%S")
        date_str = session_dt.strftime("%Y-%m-%d")
        return f"{date_str}T{trade_time}:00Z"
    except (ValueError, TypeError):
        return trade_time


def _write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(rows: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump({"audit_trail": rows, "exported_at": datetime.now().isoformat(), "count": len(rows)}, f, indent=2)
