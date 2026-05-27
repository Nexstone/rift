"""Alert system for RIFT portfolio supervisor.

Dispatches notifications on trading events via webhooks and log files.
Slack-compatible webhook format. All alerts are always written to the
log file regardless of webhook configuration.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError


ALERT_LOG = Path.home() / ".rift" / "algo" / "alerts.log"


def fire_alert(
    event_type: str,
    data: dict,
    alert_configs: list[dict],
) -> None:
    """Dispatch an alert to all configured channels.

    Args:
        event_type: One of: trade, stop_loss, health_drop, health_rotation,
                    drawdown_warning, drawdown_kill, session_died,
                    schedule_start, schedule_stop, risk_blocked
        data: Event-specific payload
        alert_configs: List of alert config dicts from portfolio.yaml
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    display_time = datetime.now().strftime("%H:%M")

    # Build human-readable message
    message = _format_message(event_type, data)

    # Always write to log file
    _write_log(display_time, event_type, message, data)

    # Dispatch to configured channels
    for config in alert_configs:
        events = config.get("events", [])
        if event_type in events or "all" in events:
            alert_type = config.get("type", "log")
            if alert_type == "webhook":
                url = config.get("url", "")
                if url:
                    _send_webhook(url, event_type, message, data, timestamp)


def _format_message(event_type: str, data: dict) -> str:
    """Build a human-readable alert message."""
    strategy = data.get("strategy", "")
    pair = data.get("pair", "")
    prefix = f"{strategy} {pair}" if strategy else ""

    if event_type == "trade":
        action = data.get("action", "")
        side = data.get("side", "").upper()
        size = data.get("size", 0)
        price = data.get("price", 0)
        pnl = data.get("pnl")
        if action == "open":
            return f"{prefix}: {side} {size:.4f} @ ${price:,.2f}"
        elif action == "close" and pnl is not None:
            result = "WIN" if pnl > 0 else "LOSS"
            return f"{prefix}: Closed {result} ${pnl:+,.2f}"
        return f"{prefix}: Trade {action}"

    elif event_type == "stop_loss":
        price = data.get("price", 0)
        pnl = data.get("pnl", 0)
        return f"{prefix}: Stop loss hit @ ${price:,.2f} (${pnl:+,.2f})"

    elif event_type == "health_drop":
        old_grade = data.get("old_grade", "?")
        new_grade = data.get("new_grade", "?")
        score = data.get("score", 0)
        return f"{prefix}: Health {old_grade} -> {new_grade} (score {score}/100)"

    elif event_type == "health_rotation":
        action = data.get("action", "paused")
        grade = data.get("grade", "?")
        return f"{prefix}: Auto-{action} — grade {grade}"

    elif event_type == "drawdown_warning":
        dd = data.get("drawdown_pct", 0)
        limit = data.get("limit_pct", 0)
        return f"Portfolio drawdown {dd:.1f}% (limit {limit:.1f}%)"

    elif event_type == "drawdown_kill":
        dd = data.get("drawdown_pct", 0)
        return f"KILL SWITCH: Portfolio drawdown {dd:.1f}% — all strategies stopped"

    elif event_type == "session_died":
        pid = data.get("pid", "?")
        return f"{prefix}: Daemon died (PID {pid})"

    elif event_type == "schedule_start":
        return f"{prefix}: Started (scheduled window)"

    elif event_type == "schedule_stop":
        return f"{prefix}: Stopped (outside scheduled window)"

    elif event_type == "risk_blocked":
        reason = data.get("reason", "exposure limit")
        return f"{prefix}: Entry blocked — {reason}"

    elif event_type == "scout_opportunity":
        coin = data.get("coin", "")
        direction = data.get("direction", "")
        score = data.get("score", 0)
        cats = data.get("categories", 0)
        conf = data.get("confidence", "")
        hold = data.get("hold_type", "")
        lev = data.get("leverage", 1)
        return f"Scout: {direction} {coin} — score {score:.3f}, {cats} categories, {conf}, {lev}x {hold}"

    return f"{event_type}: {json.dumps(data)}"


def _write_log(display_time: str, event_type: str, message: str, data: dict) -> None:
    """Append alert to the log file."""
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": display_time,
        "event": event_type,
        "message": message,
        "data": data,
        "timestamp": time.time(),
    }
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _send_webhook(
    url: str,
    event_type: str,
    message: str,
    data: dict,
    timestamp: str,
) -> None:
    """Send a Slack-compatible webhook."""
    # Emoji prefix based on event severity
    emoji = {
        "trade": ":chart_with_upwards_trend:",
        "stop_loss": ":octagonal_sign:",
        "health_drop": ":warning:",
        "health_rotation": ":rotating_light:",
        "drawdown_warning": ":warning:",
        "drawdown_kill": ":skull:",
        "session_died": ":skull:",
        "schedule_start": ":arrow_forward:",
        "schedule_stop": ":stop_button:",
        "risk_blocked": ":no_entry:",
    }.get(event_type, ":bell:")

    payload = {
        "text": f"{emoji} RIFT: {message}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*RIFT Alert* — `{event_type}`\n{message}",
                },
            },
        ],
    }

    try:
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlopen(req, timeout=5)
    except (URLError, Exception):
        pass  # Don't crash the supervisor over a webhook failure


def get_recent_alerts(limit: int = 20) -> list[dict]:
    """Read recent alerts from the log file."""
    if not ALERT_LOG.exists():
        return []
    try:
        lines = ALERT_LOG.read_text().strip().split("\n")
        alerts = []
        for line in lines[-limit:]:
            if line.strip():
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return alerts
    except Exception:
        return []
