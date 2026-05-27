"""NDJSON output protocol — single canonical emit() for all rift packages.

Replaces 9+ duplicated `_emit` functions scattered across the engine. Every
package emits structured records here; the TS CLI (and MCP server) consume
the NDJSON stream from stdout.

Behavior:
- Writes one JSON record per line to stdout, flushed immediately.
- Sanitizes NaN/Inf floats to None (JSON-safe; vanilla json.dumps would emit
  literal NaN, which is invalid per the spec and breaks parsers).
- For records with `type == "error"`, also writes a copy to stderr so the
  parent process captures errors even if it stops reading stdout.

Per-package _emit shims should be replaced with `from rift_core.output import emit`
in their respective refactor phases.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any


def sanitize_for_json(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None.

    Python's json module emits literal NaN/Infinity by default — not valid
    JSON per RFC 8259 and rejected by strict parsers (including most TS clients).
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [sanitize_for_json(v) for v in obj]
    return obj


def emit(data: dict) -> None:
    """Write one NDJSON record to stdout. Use this from every package.

    Errors (type == "error") are also mirrored to stderr so they survive
    a parent process that stops reading stdout (timeouts, crashes, etc.).
    """
    safe = sanitize_for_json(data)
    line = json.dumps(safe)
    print(line, flush=True)
    if isinstance(data, dict) and data.get("type") == "error":
        print(line, file=sys.stderr, flush=True)
