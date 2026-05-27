"""Shared Typer app and helpers used by every command module.

`app` is the single Typer instance. Every command file imports it from
here and decorates functions with `@app.command(...)`. This keeps the
user-facing surface flat (`rift sync`, `rift backtest`) while letting
the code split across files by domain.
"""

from __future__ import annotations

import json
import sys

import typer


app = typer.Typer(name="rift-engine", help="RIFT backtesting & trading engine")


def _sanitize_for_json(obj):
    """Replace NaN/Inf with None for valid JSON output."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _emit(data: dict) -> None:
    """Write a JSON line to stdout for the TS bridge to consume."""
    print(json.dumps(_sanitize_for_json(data)), flush=True)
    if data.get("type") == "error":
        print(f"Error: {data.get('msg', '')}", file=sys.stderr, flush=True)


def _hint(msg: str) -> None:
    """Emit a next-step hint. TUI can style or hide these."""
    _emit({"type": "hint", "msg": msg})
