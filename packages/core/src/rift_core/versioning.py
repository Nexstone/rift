"""Strategy versioning for RIFT algo trading.

Records a snapshot of strategy config and code hash on every algo
session start. Enables answering "what changed?" when performance shifts.

Storage: append-only NDJSON file at ~/.rift/algo/versions.jsonl
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path


VERSIONS_FILE = Path.home() / ".rift" / "algo" / "versions.jsonl"


@dataclass
class StrategyVersion:
    strategy_name: str
    version_hash: str          # SHA256 of sorted config values
    config_snapshot: dict      # frozen copy of config fields
    code_hash: str             # SHA256 of strategy source file
    recorded_at: str           # ISO timestamp
    session_key: str = ""      # which live session this was for


def record_version(strategy, session_key: str = "") -> StrategyVersion:
    """Snapshot the current strategy config and code for versioning.

    Args:
        strategy: A Strategy instance (has .config and source file)
        session_key: The live session key (e.g. trend_follow_BTC)

    Returns the StrategyVersion record.
    """
    from datetime import datetime

    # Extract config values
    config = strategy.config
    config_dict = {}
    if hasattr(config, "__dataclass_fields__"):
        for field_name in config.__dataclass_fields__:
            config_dict[field_name] = getattr(config, field_name, None)
    elif hasattr(config, "__dict__"):
        config_dict = {k: v for k, v in config.__dict__.items() if not k.startswith("_")}

    # Hash config (deterministic — sorted keys)
    config_str = json.dumps(config_dict, sort_keys=True, default=str)
    version_hash = hashlib.sha256(config_str.encode()).hexdigest()[:16]

    # Hash source code
    code_hash = ""
    try:
        source_file = inspect.getfile(strategy.__class__)
        code_hash = hashlib.sha256(Path(source_file).read_bytes()).hexdigest()[:16]
    except (TypeError, OSError):
        pass

    version = StrategyVersion(
        strategy_name=strategy.__class__.__name__,
        version_hash=version_hash,
        config_snapshot=config_dict,
        code_hash=code_hash,
        recorded_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        session_key=session_key,
    )

    # Append to version log
    _append_version(version)

    return version


def _append_version(version: StrategyVersion) -> None:
    """Append version record to the NDJSON log."""
    VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = asdict(version)
    # Make config serializable
    record["config_snapshot"] = {k: _safe_serialize(v) for k, v in record["config_snapshot"].items()}
    with open(VERSIONS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def _safe_serialize(v):
    """Convert non-JSON-serializable values."""
    if isinstance(v, (int, float, str, bool, type(None))):
        return v
    return str(v)


def get_version_history(strategy_name: str = "", limit: int = 20) -> list[dict]:
    """Read version history from the log.

    Args:
        strategy_name: Filter by strategy (empty = all)
        limit: Max records to return (most recent first)
    """
    if not VERSIONS_FILE.exists():
        return []

    versions: list[dict] = []
    for line in VERSIONS_FILE.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if strategy_name and record.get("strategy_name") != strategy_name:
                continue
            versions.append(record)
        except json.JSONDecodeError:
            pass

    return versions[-limit:]


def diff_versions(hash_a: str, hash_b: str) -> dict:
    """Compare two version snapshots and return what changed.

    Args:
        hash_a: Version hash of the earlier version
        hash_b: Version hash of the later version

    Returns dict of {field: {old: value, new: value}} for changed fields.
    """
    versions = get_version_history(limit=1000)

    config_a = None
    config_b = None
    for v in versions:
        if v["version_hash"] == hash_a:
            config_a = v.get("config_snapshot", {})
        if v["version_hash"] == hash_b:
            config_b = v.get("config_snapshot", {})

    if config_a is None or config_b is None:
        return {"error": "Version not found"}

    changes: dict = {}
    all_keys = set(list(config_a.keys()) + list(config_b.keys()))
    for key in sorted(all_keys):
        old_val = config_a.get(key)
        new_val = config_b.get(key)
        if old_val != new_val:
            changes[key] = {"old": old_val, "new": new_val}

    code_a = ""
    code_b = ""
    for v in versions:
        if v["version_hash"] == hash_a:
            code_a = v.get("code_hash", "")
        if v["version_hash"] == hash_b:
            code_b = v.get("code_hash", "")
    if code_a != code_b:
        changes["_code_changed"] = True

    return changes


def get_current_version(strategy_name: str) -> dict | None:
    """Get the most recent version for a strategy."""
    versions = get_version_history(strategy_name=strategy_name, limit=1)
    return versions[-1] if versions else None
