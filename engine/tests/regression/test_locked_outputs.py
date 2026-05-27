"""Regression tests — lock the known-good numeric outputs.

These tests catch unexpected behavior changes from refactors or library
upgrades. If a regression test fails, EITHER the math actually changed
(investigate before "fixing" the test) OR an intentional change moved
the numbers (update the locked value in this file with a commit message
documenting why).

All tests here are marked @pytest.mark.slow because they need the real
cached data at ~/.rift/raw/20250813/. Run with:
  pytest -m slow

Or skip with:
  pytest -m 'not slow'
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest


@pytest.mark.regression
def test_builder_fee_values_unchanged():
    """Revenue protection: BUILDER_ADDRESS and BUILDER_FEE_DISPLAY must NEVER
    change without explicit operator approval. They identify the on-chain
    builder code that receives fees from every live trade.
    """
    from rift_trade.builder_fee import BUILDER_ADDRESS, BUILDER_FEE_DISPLAY

    assert BUILDER_ADDRESS == "0x0916EAb573817F02b96665386c944e297A765d7C"
    assert BUILDER_FEE_DISPLAY == "0.03% perps / 1% spot"


@pytest.mark.regression
def test_integrity_hash_matches_source():
    """If the integrity seal is non-empty, it must match the actual
    SHA256[:16] of builder_fee.py. Empty seal = dev mode (test skips).

    To re-seal after editing builder_fee.py: python scripts/seal_release.py
    """
    import hashlib

    from rift_core._internal import _BUILDER_HASH
    from rift_trade import builder_fee

    if not _BUILDER_HASH:
        pytest.skip("integrity seal empty — dev mode (run scripts/seal_release.py before release)")

    src_path = Path(builder_fee.__file__)
    actual = hashlib.sha256(src_path.read_bytes()).hexdigest()[:16]
    assert actual == _BUILDER_HASH, (
        f"builder_fee.py was modified after the integrity seal was set. "
        f"Expected {_BUILDER_HASH}, got {actual}. "
        f"Re-seal with: python scripts/seal_release.py"
    )


@pytest.mark.regression
def test_more_catalog_lists_every_engine_command():
    """`rift more` shows a hand-curated catalog of every engine command.
    If a new @app.command is added to the Python engine, it must also be
    added to packages/cli/src/commands/more.ts so users can discover it.

    Equally: if the catalog has stale entries that no longer exist in the
    engine, this test fails (a missing forward would just return a Typer
    "no such command" error, not a real bug — but the catalog should not lie).
    """
    import re

    root = Path(__file__).resolve().parents[3]
    cmd_dir = root / "engine" / "src" / "rift" / "commands"
    catalog_file = root / "packages" / "cli" / "src" / "commands" / "more.ts"

    if not catalog_file.exists():
        pytest.skip(f"catalog file not found: {catalog_file}")

    # Scan engine commands
    engine_cmds: set[str] = set()
    cmd_re = re.compile(r'@app\.command\(\s*"([^"]+)"\s*\)')
    for py in cmd_dir.glob("*.py"):
        for m in cmd_re.finditer(py.read_text()):
            engine_cmds.add(m.group(1))

    # Scan catalog entries
    catalog_cmds: set[str] = set()
    catalog_re = re.compile(r"name:\s*'([^']+)'")
    for m in catalog_re.finditer(catalog_file.read_text()):
        catalog_cmds.add(m.group(1))

    missing_from_catalog = engine_cmds - catalog_cmds
    stale_in_catalog = catalog_cmds - engine_cmds

    assert not missing_from_catalog, (
        f"Engine commands not listed in `rift more` catalog "
        f"(add to packages/cli/src/commands/more.ts): {sorted(missing_from_catalog)}"
    )
    assert not stale_in_catalog, (
        f"Catalog entries that no longer exist as engine commands "
        f"(remove from packages/cli/src/commands/more.ts): {sorted(stale_in_catalog)}"
    )


@pytest.mark.regression
def test_no_modify_order_calls():
    """Builder fee policy: order placement must use cancel+replace, not
    modify_order, because a modify_order call would not carry the
    builder= parameter and would silently bypass the fee. If you genuinely
    need modify-in-place, update this allowlist explicitly so reviewers
    see the change.
    """
    import re

    root = Path(__file__).resolve().parents[3]
    scan_dirs = [
        root / "packages" / "trade" / "src",
        root / "engine" / "src" / "rift",
    ]
    allowed: set[str] = set()
    pattern = re.compile(r"\.modify(?:_order)?\(")
    bad: list[str] = []
    for d in scan_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            text = f.read_text()
            for m in pattern.finditer(text):
                rel = str(f.relative_to(root))
                if rel in allowed:
                    continue
                # Report 30 chars of context so reviewers see the call
                start = max(0, m.start() - 15)
                end = min(len(text), m.end() + 15)
                bad.append(f"{rel}: ...{text[start:end]}...")
    assert not bad, (
        "modify/modify_order calls found in trade-execution code. These "
        "bypass the builder= parameter and would skip the fee. Use "
        "cancel+replace instead, or update the allowlist in this test:\n  "
        + "\n  ".join(bad)
    )


@pytest.mark.regression
def test_strategy_registry_includes_oss_demo():
    """The OSS demo strategy must be auto-registered when CLI starts.
    This guarantees `rift init` step 4 has a valid strategy to backtest.
    """
    # Trigger SDK registration (happens automatically in rift.cli)
    import rift_strategies_sdk  # noqa: F401
    from rift_engine.strategy import _REGISTRY

    assert "trend_follow" in _REGISTRY, (
        "trend_follow must be registered — `rift init` step 4 depends on it"
    )


@pytest.mark.regression
def test_fill_schema_unchanged():
    """The canonical fill schema is part of the data-of-record contract.
    Adding columns is OK; removing or renaming would break all stored parquet."""
    from rift_core.schema import FILL_SCHEMA

    required = {
        "timestamp", "price", "size", "side", "dir",
        "is_open", "is_long", "crossed",
        "closed_pnl", "fee", "start_position",
    }
    assert required.issubset(set(FILL_SCHEMA.keys()))


@pytest.mark.slow
@pytest.mark.regression
def test_sync_cached_day_produces_locked_fill_count():
    """Locked: syncing 2025-08-13 (fully cached) produces 928,424 ETH fills
    and 114,298 SUI fills. Bit-identical across multiple test runs.

    If this changes, EITHER the S3 archive was updated retroactively (very
    unlikely) OR the parse/dedup logic changed (investigate)."""
    multiprocessing.set_start_method("spawn", force=True)

    from rift_data.s3 import download as s3_download
    from rift_data.s3 import sync as s3_sync
    from rift_data.s3 import sync_coins

    class FakeS3:
        def get_object(self, **kw):
            raise RuntimeError("FakeS3: unexpected cache miss in regression test")
    s3_download.get_s3_client = lambda: FakeS3()
    s3_sync.get_s3_client = lambda: FakeS3()

    results = sync_coins(
        coins=["ETH", "SUI"],
        timeframes=["5m", "1h"],
        start_date="2025-08-13",
        end_date="2025-08-13",
        include_funding=False,
        incremental=False,
        max_download_workers=4,
        max_parse_workers=4,
    )

    assert results["ETH"]["fills"] == 928_424, "ETH 2025-08-13 fill count changed"
    assert results["SUI"]["fills"] == 114_298, "SUI 2025-08-13 fill count changed"


@pytest.mark.slow
@pytest.mark.regression
def test_backtest_trend_follow_oss_demo_runs_cleanly():
    """The OSS demo strategy must produce a valid BacktestResult on the
    BTC 4h cache. This is what `rift init` step 4 runs — if it fails,
    new users see a broken first-run experience.
    """
    import subprocess, json
    result = subprocess.run(
        [str(Path.home() / "rift" / "engine" / ".venv" / "bin" / "rift-engine"),
         "backtest", "trend_follow", "--pair", "BTC", "--tf", "4h"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"trend_follow backtest failed: {result.stderr[:500]}"

    bt = None
    for line in result.stdout.strip().split("\n"):
        try:
            d = json.loads(line)
            if d.get("type") == "result":
                bt = d
        except json.JSONDecodeError:
            continue
    assert bt is not None
    # Just sanity-check that it produced trades and emitted required fields
    assert bt["num_trades"] > 0, "trend_follow produced no trades — broken init experience"
    assert "total_return_pct" in bt
    assert "sharpe_ratio" in bt
