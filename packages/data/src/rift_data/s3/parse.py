"""JSONL parser — extracts per-coin fills from a decompressed hourly file.

Designed as a ProcessPool worker (`parse_hour_file`) because JSON parsing is
CPU-bound and threads don't help (CPython GIL). Each worker reads its cache
file from disk, parses with orjson if available (stdlib json otherwise),
filters to the target coin set, and returns per-coin fill tuples.

Result fills are 11-tuples; `fills_list_to_df` converts them to the canonical
FILL_SCHEMA DataFrame.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from rift_core.schema import FILL_SCHEMA


def _extract_coin_fills(raw: bytes, coins: set[str]) -> dict[str, list]:
    """Parse JSONL bytes and extract fills for the target coin set.

    Operates on bytes directly (no decode-to-str copy). Uses orjson when
    available (~3-5x faster than stdlib json).
    """
    try:
        import orjson  # type: ignore
        loads = orjson.loads
    except ImportError:
        loads = json.loads

    results: dict[str, list] = {c: [] for c in coins}

    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = loads(line)
        except Exception:
            continue
        for evt in record.get("events", []):
            if isinstance(evt, list) and len(evt) >= 2:
                fill = evt[1]
            elif isinstance(evt, dict):
                fill = evt
            else:
                continue

            fill_coin = fill.get("coin", "")
            if fill_coin not in coins:
                continue

            ts = fill.get("time", 0)
            try:
                ts_ms = int(ts)
            except (ValueError, TypeError):
                continue

            dir_raw = fill.get("dir", "")
            results[fill_coin].append((
                ts_ms,
                float(fill.get("px", 0)),
                float(fill.get("sz", 0)),
                fill.get("side", ""),
                dir_raw,
                "Open" in dir_raw,
                "Long" in dir_raw,
                bool(fill.get("crossed", False)),
                float(fill.get("closedPnl", 0)),
                float(fill.get("fee", 0)),
                float(fill.get("startPosition", 0)),
            ))
    return results


def parse_hour_file(cache_path_str: str, coins_list: list[str]) -> dict[str, list]:
    """ProcessPool worker: read decompressed JSONL from disk, return per-coin fills.

    Module-level so it pickles cleanly under macOS spawn start method.
    Returns dict[s3_coin_name] -> list of fill tuples (only target coins).
    """
    raw = Path(cache_path_str).read_bytes()
    return _extract_coin_fills(raw, set(coins_list))


def _fills_list_to_df(fills: list) -> pl.DataFrame:
    """Convert list of 11-tuples to a DataFrame with the canonical fill schema."""
    if not fills:
        return pl.DataFrame(schema=FILL_SCHEMA)

    ts, px, sz, sd, dr, io_, il, cr, cp, fe, sp = zip(*fills)
    return pl.DataFrame({
        "timestamp": list(ts),
        "price": list(px),
        "size": list(sz),
        "side": list(sd),
        "dir": list(dr),
        "is_open": list(io_),
        "is_long": list(il),
        "crossed": list(cr),
        "closed_pnl": list(cp),
        "fee": list(fe),
        "start_position": list(sp),
    })
